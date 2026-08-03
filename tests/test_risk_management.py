from datetime import datetime, timezone

import pytest

from kalshi_bots.skills.risk_management import (
    MIN_EDGE_CENTS, RiskManager, RiskError, RiskUnknownSkill, kelly_fraction,
    required_edge_cents,
)
from kalshi_bots.skills.kalshi_client import est_fee_cents
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import (
    CryptoSignal, Fill, MarketRef, Settlement, SizingRequest, WindowRef,
)

NOW = datetime(2026, 7, 23, 1, 20, tzinfo=timezone.utc)


class FakeKalshi:
    def __init__(self, balance=50_000):
        self.balance = balance
        self.positions = []
        self.settlements: dict[str, list] = {}

    def get_balance(self):
        if self.balance is None:
            raise RuntimeError("api down")
        return self.balance

    def get_positions(self):
        return self.positions

    def get_settlements(self, ticker):
        return self.settlements.get(ticker, [])


def market(ticker="KXBTC15M-26JUL222130-30"):
    return MarketRef(family="crypto", series_ticker="KXBTC15M",
                     event_ticker=ticker.rsplit("-", 1)[0], market_ticker=ticker,
                     yes_label="up", title="", close_ts=None,
                     settlement_notes=None)


def req(skill="btc-15min-fair-value", price=60, prob=0.68, depth=1000,
        eid="KXBTC15M-26JUL222130", ticker="KXBTC15M-26JUL222130-30",
        is_live=True, recent_move_pct=None):
    window = WindowRef(series_ticker="KXBTC15M", event_ticker=eid,
                       market_ticker=ticker,
                       opens_at=NOW, closes_at=NOW, strike=66010.86)
    sig = CryptoSignal(signal_type="fair-value-candidate",
                       series_ticker="KXBTC15M", market_ticker=ticker,
                       window=window, phase="midpoint", payload={},
                       emitted_at=NOW)
    return SizingRequest(skill_name=skill, market=market(ticker),
                         side="yes", entry_price=price, model_prob=prob,
                         book_depth_at_entry=depth, signal=sig,
                         event_id=eid, is_live=is_live,
                         recent_move_pct=recent_move_pct)


def fill(contracts, price, fee=0, side="yes", action="buy"):
    return Fill(order_id="o1", market_ticker="T", side=side, action=action,
                contracts=contracts, price=price, taker_fee_cents=fee, ts=NOW,
                raw={})


@pytest.fixture
def rm(tmp_path):
    root = tmp_path / "vault"
    (root / "03-market-context").mkdir(parents=True)
    return RiskManager(Vault(root=str(root)), FakeKalshi())


@pytest.fixture
def rm_uncapped(rm, monkeypatch):
    """The %-cap tests need the draft-skill per-window contract cap out of the
    way; otherwise 20 contracts binds before any percentage cap can."""
    monkeypatch.setattr(
        "kalshi_bots.skills.risk_management.MAX_CONTRACTS_PER_WINDOW", 10_000)
    return rm


class TestRequiredEdgeCents:
    def test_at_the_money_clears_round_trip_fee(self):
        # 50c coin flip: per-side taker fee is 2c, round-trip 4c, +1c buffer
        # = 5c, which exceeds the flat 4c floor -> the fee-aware bar wins
        assert required_edge_cents(50) == 2 * est_fee_cents(1, 50) + 1
        assert required_edge_cents(50) == 5

    def test_deep_wings_fall_back_to_flat_floor(self):
        # at 10c the round-trip fee (1c*2) + buffer = 3c < MIN_EDGE_CENTS floor
        assert required_edge_cents(10) == MIN_EDGE_CENTS

    def test_never_below_the_flat_floor(self):
        for price in range(1, 100):
            assert required_edge_cents(price) >= MIN_EDGE_CENTS

    def test_fee_bar_is_highest_at_the_money(self):
        # the quadratic fee peaks at 50c, so the required edge should too
        assert required_edge_cents(50) >= required_edge_cents(25)
        assert required_edge_cents(50) >= required_edge_cents(75)


class TestKellyMath:
    def test_worked_example(self, rm_uncapped):
        """Spec worked example: p=0.68, c=60, bankroll 50,000c -> 40 contracts
        (per-trade 5% cap binds before the depth gate)."""
        r = rm_uncapped.size(req())
        assert kelly_fraction(0.68, 60) == pytest.approx(0.20)
        assert r.contracts == 40
        assert r.limit_price == 60
        assert r.capped_by == ["per_trade_cap"]
        assert r.est_fee_cents_total == 68

    def test_no_edge_price_equals_prob(self, rm):
        r = rm.size(req(price=68, prob=0.68))
        assert r.contracts == 0 and r.capped_by == ["no_edge"]

    def test_fee_pushes_out_edge(self, rm):
        # p=0.61, c=60: gross edge 1c but fee 2c -> c_f=62 > 61 -> no edge
        r = rm.size(req(price=60, prob=0.61))
        assert r.capped_by == ["no_edge"]

    def test_unknown_skill_raises(self, rm):
        with pytest.raises(RiskUnknownSkill):
            rm.size(req(skill="mystery-skill"))

    def test_balance_failure_raises(self, tmp_path):
        (tmp_path / "v" / "03-market-context").mkdir(parents=True)
        rm = RiskManager(Vault(root=str(tmp_path / "v")), FakeKalshi(balance=None))
        with pytest.raises(RiskError):
            rm.size(req())

    def test_vol_spike_quarter_kelly_and_3pct_cap(self, rm_uncapped):
        r = rm_uncapped.size(req(skill="btc-15min-vol-spike", depth=1000))
        # f = 0.20 * 0.5 * 0.5 = 0.05 -> capped at 3%
        assert r.capped_by == ["per_trade_cap"]
        assert r.contracts == int(0.03 * 50_000) // 62

    def test_non_live_multiplier_mechanism(self, rm_uncapped, monkeypatch):
        # 24/7 crypto keeps everything live; the (live, non_live) mechanism is
        # kept and tested here via an injected table entry, not real params
        monkeypatch.setitem(
            __import__("kalshi_bots.skills.risk_management",
                       fromlist=["SKILL_RISK_MULTIPLIER"]).SKILL_RISK_MULTIPLIER,
            "btc-15min-fair-value", (1.0, 0.5))
        r_live = rm_uncapped.size(req(eid="e1", ticker="T1-00"))
        rm_uncapped.cancel_intent("T1-00", "btc-15min-fair-value")
        r_non = rm_uncapped.size(req(is_live=False, eid="e2", ticker="T2-15"))
        assert r_live.kelly_fraction_used == pytest.approx(0.10)
        assert r_non.kelly_fraction_used == pytest.approx(0.05)


class TestFlatSizing:
    def test_flat_sizing_mechanism(self, rm_uncapped, monkeypatch):
        # no crypto skill flat-sizes yet; the mechanism (multiplier None ->
        # per-trade-cap fraction, Kelly forbidden) stays covered via injection
        monkeypatch.setitem(
            __import__("kalshi_bots.skills.risk_management",
                       fromlist=["SKILL_RISK_MULTIPLIER"]).SKILL_RISK_MULTIPLIER,
            "btc-15min-fair-value", None)
        r = rm_uncapped.size(req(price=93, prob=0.99, depth=2000))
        # budget 5% of 50000 = 2500; c_f = 93 + ceil(7*93*7/10000)=93+1=94
        assert r.kelly_fraction_used is None
        assert r.contracts == 2500 // 94


class TestCaps:
    def test_per_window_contract_cap(self, rm):
        # draft-skill training wheels: 40 contracts of budget -> capped at 20
        r = rm.size(req())
        assert r.contracts == 20
        assert "per_window_contract_cap" in r.capped_by

    def test_correlation_same_event(self, rm_uncapped):
        rm_uncapped.size(req(eid="E1", ticker="T1-00"))
        r2 = rm_uncapped.size(req(skill="btc-15min-orderflow-imbalance",
                                  eid="E1", ticker="T1B-00"))
        assert "correlation_same_event" in r2.capped_by

    def test_same_market_counts_as_same_event(self, rm_uncapped):
        # two skills entering the same market ticker correlate even if the
        # event id string differs (label overlap)
        rm_uncapped.size(req(eid="E1", ticker="T1-00"))
        r2 = rm_uncapped.size(req(skill="btc-15min-orderflow-imbalance",
                                  eid="E-OTHER", ticker="T1-00"))
        assert "correlation_same_event" in r2.capped_by

    def test_per_event_cap_binds(self, rm_uncapped):
        rm_uncapped.size(req(eid="E1", ticker="T1-00"))  # reserves ~5% on E1
        r2 = rm_uncapped.size(req(skill="btc-15min-orderflow-imbalance",
                                  eid="E1", ticker="T1-15"))
        assert "per_event_cap" in r2.capped_by
        assert r2.contracts == 0  # event already at the 5% cap

    def test_total_exposure_cap(self, rm_uncapped):
        # fill 3 events at ~5% each -> 15% total; 4th entry has no room
        for i in range(3):
            r = rm_uncapped.size(req(eid=f"E{i}", ticker=f"T{i}-00"))
            assert r.contracts > 0
        r4 = rm_uncapped.size(req(eid="E9", ticker="T9-00"))
        assert "total_exposure_cap" in r4.capped_by
        # fee rounding may leave a 1-contract crumb below the cap; the cap must
        # have bound the budget either way
        assert r4.contracts <= 1

    def test_max_open_positions(self, rm_uncapped, monkeypatch):
        monkeypatch.setattr("kalshi_bots.skills.risk_management.TOTAL_EXPOSURE_CAP_PCT", 100)
        monkeypatch.setattr("kalshi_bots.skills.risk_management.PER_EVENT_EXPOSURE_CAP_PCT", 100)
        for i in range(6):
            r = rm_uncapped.size(req(eid=f"E{i}", ticker=f"T{i}-00"))
            assert r.contracts > 0, f"position {i}: {r.capped_by}"
        r7 = rm_uncapped.size(req(eid="E9", ticker="T9-00"))
        assert r7.capped_by[-1] == "max_open_positions"

    def test_depth_min_gate(self, rm):
        r = rm.size(req(depth=99))  # fair-value needs 100
        assert r.contracts == 0 and r.capped_by[-1] == "depth_min"

    def test_depth_consumption_gate(self, rm_uncapped):
        r = rm_uncapped.size(req(depth=100))
        # per-trade 5% = 2500c budget -> 2500//62 = 40 contracts, but the
        # depth cap allows 0.25*100 = 25
        assert r.contracts == 25
        assert "depth_gate" in r.capped_by

    def test_velocity_scale_halves_a_fast_move(self, rm_uncapped):
        # a 0.6% recent move breaches the 0.4% threshold -> fraction halved,
        # so the fair-value worked example's 40 contracts becomes ~20
        r = rm_uncapped.size(req(recent_move_pct=0.006))
        assert "velocity_scale" in r.capped_by
        assert r.contracts == 20

    def test_velocity_scale_ignores_a_slow_move(self, rm_uncapped):
        r = rm_uncapped.size(req(recent_move_pct=0.001))
        assert "velocity_scale" not in r.capped_by
        assert r.contracts == 40

    def test_velocity_scale_is_direction_agnostic(self, rm_uncapped):
        # a fast move DOWN scales sizing the same as a fast move up
        r = rm_uncapped.size(req(recent_move_pct=-0.006))
        assert "velocity_scale" in r.capped_by
        assert r.contracts == 20

    def test_velocity_scale_skipped_when_unavailable(self, rm_uncapped):
        r = rm_uncapped.size(req(recent_move_pct=None))
        assert "velocity_scale" not in r.capped_by
        assert r.contracts == 40


class TestHalts:
    def test_manual_halt(self, rm):
        rm.set_halt(True, "testing", caller="discord")
        r = rm.size(req())
        assert r.capped_by[-1] == "halted"

    def test_daily_losses_do_not_block_sizing(self, rm):
        # Owner-directed 2026-08-02: the automatic daily-loss halt was
        # removed. Any realized daily loss must leave sizing unaffected.
        day = rm._et_today()
        rm._daily_pnl[day] = -25_000  # 50% of the 50k bankroll
        r = rm.size(req())
        assert r.contracts > 0
        assert "daily_loss_halt" not in r.capped_by

    def test_halt_persists_restart(self, rm, tmp_path):
        rm.set_halt(True, "manual", caller="discord")
        rm2 = RiskManager(rm.vault, FakeKalshi())
        assert rm2.halted()[0] is True


class TestLedgerLifecycle:
    def test_fill_exit_settle(self, rm):
        m = market()
        rm.on_fill(fill(40, 60, fee=68), m, "btc-15min-fair-value", "E1")
        assert rm.exposure().open_cost_cents == 40 * 60 + 68
        rm.on_exit(fill(40, 70, fee=0, action="sell"), m, "btc-15min-fair-value")
        exp = rm.exposure()
        assert exp.open_positions == 0
        assert exp.daily_realized_pnl_cents == 40 * 70 - (40 * 60 + 68)

    def test_exit_fill_labeled_as_other_side_is_flipped_into_position_terms(self, rm):
        """Regression from the 2026-07-29 live session: Kalshi labels the
        fill that closes a NO position as a YES-side trade priced in
        yes-cents (closing NO == buying YES). Netting that raw yes price
        against a no-terms basis inflated proceeds — a NO bought at 67c and
        closed at 66c (yes price 34c) must book roughly -1c/contract, not
        +34c-ish."""
        m = market()
        rm.on_fill(fill(2, 67, fee=3, side="no"), m, "btc-15min-fair-value", "E1")
        pnl = rm.on_exit(fill(2, 34, fee=3, side="yes", action="buy"),
                         m, "btc-15min-fair-value")
        # basis 2*67+3=137; proceeds 2*(100-34)-3=129
        assert pnl == -8
        assert rm.exposure().open_positions == 0

    def test_settle_win(self, rm):
        m = market()
        rm.on_fill(fill(10, 93, fee=7), m, "btc-15min-fair-value", "E1")
        rm.on_settle(Settlement(market_ticker=m.market_ticker, result="yes",
                                settled_ts=NOW, revenue_cents=1000, raw={}),
                     m, "btc-15min-fair-value")
        assert rm.exposure().daily_realized_pnl_cents == 1000 - 937

    def test_reconcile_mismatch_halts(self, rm):
        rm.kalshi.positions = []  # ledger empty too -> ok
        assert rm.reconcile() is True
        from kalshi_bots.types import Position
        rm.kalshi.positions = [Position("GHOST", "yes", 5, 50, 0, {})]
        assert rm.reconcile() is False
        assert rm.halted()[0] is True

    def test_reconcile_self_heals_settled_win(self, rm):
        """A ledger position missing from live, with a matching settlement,
        is a restart-while-the-window-settled case: self-heal, don't halt."""
        m = market("T-SETTLED")
        rm.on_fill(fill(5, 52, fee=8), m, "btc-15min-fair-value", "E1")
        rm.kalshi.positions = []  # nothing live: it settled
        rm.kalshi.settlements["T-SETTLED"] = [
            Settlement(market_ticker="T-SETTLED", result="yes", settled_ts=NOW,
                      revenue_cents=999999, raw={})  # revenue_cents must be ignored
        ]
        assert rm.reconcile() is True
        assert rm.halted()[0] is False
        assert "T-SETTLED" not in rm._positions
        assert rm.last_reconcile_settled["T-SETTLED"].result == "yes"
        # pnl from ledger's own cost basis (5*100 - (5*52+8)=232), not revenue_cents
        assert rm.exposure().daily_realized_pnl_cents == 232

    def test_reconcile_self_heals_settled_loss(self, rm):
        m = market("T-LOST")
        rm.on_fill(fill(7, 26, fee=9), m, "btc-15min-fair-value", "E1")
        rm.kalshi.positions = []
        rm.kalshi.settlements["T-LOST"] = [
            Settlement(market_ticker="T-LOST", result="no", settled_ts=NOW,
                      revenue_cents=0, raw={})
        ]
        assert rm.reconcile() is True
        assert rm.exposure().daily_realized_pnl_cents == -(7 * 26 + 9)

    def test_reconcile_halts_on_unexplained_missing_position(self, rm):
        """Missing from live and no settlement record yet -> still halt."""
        m = market("T-UNKNOWN")
        rm.on_fill(fill(3, 40, fee=1), m, "btc-15min-fair-value", "E1")
        rm.kalshi.positions = []
        assert rm.reconcile() is False
        assert rm.halted()[0] is True
        assert "T-UNKNOWN" in rm._positions  # not popped: nothing to explain it away

    def test_reconcile_clears_a_halt_it_set_once_resolved(self, rm):
        """A halt reconcile set for an unexplained mismatch lifts on its own
        the moment a settlement record shows up (e.g. Kalshi settles it a
        minute after the first restart-triggered reconcile ran)."""
        m = market("T-SLOW-SETTLE")
        rm.on_fill(fill(4, 30, fee=2), m, "btc-15min-fair-value", "E1")
        rm.kalshi.positions = []
        assert rm.reconcile() is False  # no settlement yet -> halt
        assert rm.halted()[0] is True
        rm.kalshi.settlements["T-SLOW-SETTLE"] = [
            Settlement(market_ticker="T-SLOW-SETTLE", result="yes", settled_ts=NOW,
                      revenue_cents=0, raw={})
        ]
        assert rm.reconcile() is True
        assert rm.halted() == (False, None)

    def test_reconcile_does_not_clear_a_manual_halt(self, rm):
        rm.set_halt(True, "operator called it", caller="discord")
        assert rm.reconcile() is True  # nothing live, nothing in ledger: clean
        assert rm.halted()[0] is True  # but a human set this — stays halted


class TestIntentSerialization:
    def test_headroom_for_one(self, rm_uncapped):
        """Two size() calls against headroom for ~one position: the second must
        see the first's intent reservation."""
        r1 = rm_uncapped.size(req(eid="E1", ticker="T1-00"))
        assert r1.contracts > 0
        # same event, different skill: per-event cap sees the intent
        r2 = rm_uncapped.size(req(skill="btc-15min-orderflow-imbalance",
                                  eid="E1", ticker="T1-15"))
        assert r2.contracts == 0

    def test_cancel_intent_releases(self, rm_uncapped):
        rm_uncapped.size(req(eid="E1", ticker="T1-00"))
        rm_uncapped.cancel_intent("T1-00", "btc-15min-fair-value")
        r2 = rm_uncapped.size(req(skill="btc-15min-orderflow-imbalance",
                                  eid="E1", ticker="T1-15"))
        # with the intent gone, per-event room is back
        assert r2.contracts > 0


class TestOverrides:
    def test_current_falls_back_to_baseline(self):
        from kalshi_bots.skills import risk_management as risk_mod
        assert risk_mod.current("MIN_EDGE_CENTS") == risk_mod.MIN_EDGE_CENTS
        assert risk_mod.current("SKILL_RISK_MULTIPLIER",
                                skill="btc-15min-fair-value") == (1.0, 1.0)

    def test_current_respects_monkeypatched_baseline(self, monkeypatch):
        """The rm_uncapped fixture pattern must keep working: current() reads
        the live module constant, never a frozen copy."""
        from kalshi_bots.skills import risk_management as risk_mod
        monkeypatch.setattr(
            "kalshi_bots.skills.risk_management.MIN_EDGE_CENTS", 7)
        assert risk_mod.current("MIN_EDGE_CENTS") == 7

    def test_override_changes_sizing(self, rm_uncapped):
        from kalshi_bots.skills import risk_management as risk_mod
        base = rm_uncapped.size(req(eid="E1", ticker="T1-00"))
        rm_uncapped.cancel_intent("T1-00", "btc-15min-fair-value")
        risk_mod.set_override("PER_TRADE_CAP_PCT", 2,
                              skill="btc-15min-fair-value",
                              reason="test", caller="tuner")
        tightened = rm_uncapped.size(req(eid="E2", ticker="T2-00"))
        assert 0 < tightened.contracts < base.contracts

    def test_out_of_corridor_on_tighten_side_still_raises(self):
        # Tighten-direction bounds are untouched by the 2026-07-30 relax
        # change: these still reject.
        from kalshi_bots.skills import risk_management as risk_mod
        with pytest.raises(risk_mod.RiskOverrideError):
            risk_mod.set_override("MIN_EDGE_CENTS", 9,   # above 2x tighten ceiling
                                  reason="t", caller="tuner")
        assert not risk_mod.has_override("MIN_EDGE_CENTS")
        with pytest.raises(risk_mod.RiskOverrideError):
            risk_mod.set_override("TOTAL_EXPOSURE_CAP_PCT", 3,  # below 0.5x floor
                                  reason="t", caller="tuner")
        assert risk_mod.active_overrides() == {}

    def test_looser_than_baseline_now_allowed_on_relax_side(self):
        # 2026-07-30 owner-directed change: relax is no longer capped at
        # baseline. A value looser than baseline is now a valid override on
        # the relax side (still subject to each param's own domain floor).
        from kalshi_bots.skills import risk_management as risk_mod
        risk_mod.set_override("MIN_EDGE_CENTS", 3,   # below baseline (4): looser
                              reason="t", caller="tuner")
        assert risk_mod.current("MIN_EDGE_CENTS") == 3
        risk_mod.set_override("PER_TRADE_CAP_PCT", 6,  # above baseline (5): looser
                              skill="btc-15min-fair-value",
                              reason="t", caller="tuner")
        assert risk_mod.current("PER_TRADE_CAP_PCT",
                                skill="btc-15min-fair-value") == 6
        with pytest.raises(risk_mod.RiskOverrideError):
            risk_mod.set_override("MIN_EDGE_CENTS", -1,  # domain floor: no negative edge
                                  reason="t", caller="tuner")

    def test_non_tunable_names_rejected(self):
        from kalshi_bots.skills import risk_management as risk_mod
        with pytest.raises(risk_mod.RiskOverrideError):
            risk_mod.set_override("INTENT_TTL_S", 60, reason="t", caller="tuner")
        with pytest.raises(risk_mod.RiskOverrideError):
            risk_mod.set_override("NOT_A_PARAM", 1, reason="t", caller="tuner")

    def test_tuple_param_validated_elementwise(self):
        from kalshi_bots.skills import risk_management as risk_mod
        risk_mod.set_override("SKILL_RISK_MULTIPLIER", (0.5, 0.5),
                              skill="btc-15min-fair-value",
                              reason="t", caller="tuner")
        assert risk_mod.current("SKILL_RISK_MULTIPLIER",
                                skill="btc-15min-fair-value") == (0.5, 0.5)
        # Above baseline (1.0) is now a valid relax-side value -- no ceiling
        # since 2026-07-30.
        risk_mod.set_override("SKILL_RISK_MULTIPLIER", (1.5, 1.2),
                              skill="btc-15min-fair-value",
                              reason="t", caller="tuner")
        assert risk_mod.current("SKILL_RISK_MULTIPLIER",
                                skill="btc-15min-fair-value") == (1.5, 1.2)
        # Below the tighten floor (0.25x baseline) still rejects -- that
        # bound is untouched by the relax change.
        with pytest.raises(risk_mod.RiskOverrideError):
            risk_mod.set_override("SKILL_RISK_MULTIPLIER", (0.1, 1.0),
                                  skill="btc-15min-fair-value",
                                  reason="t", caller="tuner")

    def test_clear_override_retracks_baseline(self):
        from kalshi_bots.skills import risk_management as risk_mod
        risk_mod.set_override("MIN_EDGE_CENTS", 6, reason="t", caller="tuner")
        assert risk_mod.current("MIN_EDGE_CENTS") == 6
        assert risk_mod.clear_override("MIN_EDGE_CENTS") is True
        assert risk_mod.current("MIN_EDGE_CENTS") == risk_mod.MIN_EDGE_CENTS
        assert risk_mod.clear_override("MIN_EDGE_CENTS") is False

    def test_active_overrides_snapshot_and_log(self):
        from kalshi_bots.skills import risk_management as risk_mod
        risk_mod.set_override("MIN_EDGE_CENTS", 6, reason="t", caller="tuner")
        risk_mod.set_override("PER_TRADE_CAP_PCT", 3,
                              skill="btc-15min-fair-value",
                              reason="t", caller="tuner")
        assert risk_mod.active_overrides() == {
            "MIN_EDGE_CENTS": 6,
            "PER_TRADE_CAP_PCT|btc-15min-fair-value": 3,
        }
        assert len(risk_mod.override_log) == 2
        assert risk_mod.override_log[-1]["caller"] == "tuner"
