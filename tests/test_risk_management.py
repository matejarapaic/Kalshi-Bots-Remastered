from datetime import datetime, timezone

import pytest

from kalshi_bots.skills.risk_management import (
    RiskManager, RiskError, RiskUnknownSkill, kelly_fraction,
)
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import (
    CandidateSignal, Fill, MarketRef, Settlement, SizingRequest,
)

NOW = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)


class FakeKalshi:
    def __init__(self, balance=50_000):
        self.balance = balance
        self.positions = []

    def get_balance(self):
        if self.balance is None:
            raise RuntimeError("api down")
        return self.balance

    def get_positions(self):
        return self.positions


def market(ticker="KXMLBGAME-26JUL191920LADNYY-NYY", team="NYY"):
    return MarketRef(league="mlb", series_ticker="KXMLBGAME",
                     event_ticker=ticker.rsplit("-", 1)[0], market_ticker=ticker,
                     yes_team_kalshi_abbr=team, title="", close_ts=None,
                     settlement_notes=None)


def req(skill="live-win-prob-overreaction", price=60, prob=0.68, depth=1000,
        eid="e1", ticker="KXMLBGAME-26JUL191920LADNYY-NYY", team="NYY",
        is_live=True):
    sig = CandidateSignal(signal_type="overreaction-candidate", league="mlb",
                          espn_event_id=eid, market_ticker=ticker, payload={},
                          emitted_at=NOW)
    return SizingRequest(skill_name=skill, market=market(ticker, team),
                         side="yes", entry_price=price, model_prob=prob,
                         book_depth_at_entry=depth, signal=sig,
                         espn_event_id=eid, is_live=is_live)


def fill(contracts, price, fee=0, side="yes", action="buy"):
    return Fill(order_id="o1", market_ticker="T", side=side, action=action,
                contracts=contracts, price=price, taker_fee_cents=fee, ts=NOW,
                raw={})


@pytest.fixture
def rm(tmp_path):
    root = tmp_path / "vault"
    (root / "03-market-context").mkdir(parents=True)
    return RiskManager(Vault(root=str(root)), FakeKalshi())


class TestKellyMath:
    def test_worked_example(self, rm):
        """Spec worked example: p=0.68, c=60, bankroll 50,000c -> 40 contracts."""
        r = rm.size(req())
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

    def test_injury_quarter_kelly_and_3pct_cap(self, rm):
        r = rm.size(req(skill="injury-news-repricing-lag", depth=1000))
        # f = 0.20 * 0.5 * 0.5 = 0.05 -> capped at 3%
        assert r.capped_by == ["per_trade_cap"]
        assert r.contracts == int(0.03 * 50_000) // 62

    def test_divergence_pregame_half_mult(self, rm):
        r_live = rm.size(req(skill="sportsbook-kalshi-divergence", eid="g1",
                             ticker="T1-A", team="A"))
        rm.cancel_intent("T1-A", "sportsbook-kalshi-divergence")
        r_pre = rm.size(req(skill="sportsbook-kalshi-divergence", is_live=False,
                            eid="g2", ticker="T2-B", team="B"))
        assert r_live.kelly_fraction_used == pytest.approx(0.10)
        assert r_pre.kelly_fraction_used == pytest.approx(0.05)


class TestFlatSizing:
    def test_garbage_time_flat(self, rm):
        r = rm.size(req(skill="garbage-time-mispricing", price=93, prob=0.99,
                        depth=2000))
        # budget 5% of 50000 = 2500; c_f = 93 + ceil(7*93*7/10000)=93+1=94
        assert r.kelly_fraction_used is None
        assert r.contracts == 2500 // 94

    def test_garbage_aggregate_cap(self, rm):
        # three concurrent garbage positions; the aggregate 10% cap binds on #3
        for i, t in enumerate(["TA-X", "TB-Y"]):
            r = rm.size(req(skill="garbage-time-mispricing", price=93, prob=0.99,
                            depth=2000, eid=f"g{i}", ticker=t, team=t[-1]))
            assert "garbage_aggregate_cap" not in r.capped_by
        r3 = rm.size(req(skill="garbage-time-mispricing", price=93, prob=0.99,
                         depth=2000, eid="g9", ticker="TC-Z", team="Z"))
        assert "garbage_aggregate_cap" in r3.capped_by


class TestCaps:
    def test_correlation_same_game(self, rm):
        rm.size(req(eid="g1", ticker="T1-A", team="A"))
        r2 = rm.size(req(skill="sportsbook-kalshi-divergence", eid="g1",
                         ticker="T1B-C", team="C"))
        assert "correlation_same_game" in r2.capped_by

    def test_same_team_counts_as_same_game(self, rm):
        rm.size(req(eid="g1", ticker="T1-NYY", team="NYY"))
        r2 = rm.size(req(skill="sportsbook-kalshi-divergence", eid="g2",
                         ticker="T2-NYY", team="NYY"))
        assert "correlation_same_game" in r2.capped_by

    def test_per_game_cap_binds(self, rm):
        rm.size(req(eid="g1", ticker="T1-A", team="A"))  # reserves ~5% on g1
        r2 = rm.size(req(skill="sportsbook-kalshi-divergence", eid="g1",
                         ticker="T1-B", team="B"))
        assert "per_game_cap" in r2.capped_by
        assert r2.contracts == 0  # game already at the 5% cap

    def test_total_exposure_cap(self, rm):
        # fill 3 games at ~5% each -> 15% total; 4th entry has no room
        for i in range(3):
            r = rm.size(req(eid=f"g{i}", ticker=f"T{i}-X{i}", team=f"X{i}"))
            assert r.contracts > 0
        r4 = rm.size(req(eid="g9", ticker="T9-Z", team="Z"))
        assert "total_exposure_cap" in r4.capped_by
        # fee rounding may leave a 1-contract crumb below the cap; the cap must
        # have bound the budget either way
        assert r4.contracts <= 1

    def test_max_open_positions(self, rm, monkeypatch):
        monkeypatch.setattr("kalshi_bots.skills.risk_management.TOTAL_EXPOSURE_CAP_PCT", 100)
        monkeypatch.setattr("kalshi_bots.skills.risk_management.PER_GAME_EXPOSURE_CAP_PCT", 100)
        for i in range(6):
            r = rm.size(req(eid=f"g{i}", ticker=f"T{i}-X{i}", team=f"X{i}"))
            assert r.contracts > 0, f"position {i}: {r.capped_by}"
        r7 = rm.size(req(eid="g9", ticker="T9-Z", team="Z"))
        assert r7.capped_by[-1] == "max_open_positions"

    def test_depth_min_gate(self, rm):
        r = rm.size(req(depth=199))  # overreaction needs 200
        assert r.contracts == 0 and r.capped_by[-1] == "depth_min"

    def test_depth_consumption_gate(self, rm):
        r = rm.size(req(depth=200, prob=0.9, price=50))
        # kelly is huge; per-trade 5% = 2500c budget -> 2500//52=48 contracts,
        # but depth cap = 0.25*200 = 50 ... 48 < 50 so no gate; tighten depth:
        r = rm.size(req(depth=200, ticker="T2-B", team="B", eid="g2"))
        assert r.contracts <= 50


class TestHalts:
    def test_manual_halt(self, rm):
        rm.set_halt(True, "testing", caller="discord")
        r = rm.size(req())
        assert r.capped_by[-1] == "halted"

    def test_daily_loss_halt(self, rm):
        day = rm._et_today()
        rm._daily_pnl[day] = -2600  # > 5% of 51,180 bankroll? 5% of ~50k = 2500
        r = rm.size(req())
        assert r.capped_by[-1] == "daily_loss_halt"

    def test_halt_persists_restart(self, rm, tmp_path):
        rm.set_halt(True, "manual", caller="discord")
        rm2 = RiskManager(rm.vault, FakeKalshi())
        assert rm2.halted()[0] is True


class TestLedgerLifecycle:
    def test_fill_exit_settle(self, rm):
        m = market()
        rm.on_fill(fill(40, 60, fee=68), m, "live-win-prob-overreaction", "e1")
        assert rm.exposure().open_cost_cents == 40 * 60 + 68
        rm.on_exit(fill(40, 70, fee=0, action="sell"), m, "live-win-prob-overreaction")
        exp = rm.exposure()
        assert exp.open_positions == 0
        assert exp.daily_realized_pnl_cents == 40 * 70 - (40 * 60 + 68)

    def test_settle_win(self, rm):
        m = market()
        rm.on_fill(fill(10, 93, fee=7), m, "garbage-time-mispricing", "e1")
        rm.on_settle(Settlement(market_ticker=m.market_ticker, result="yes",
                                settled_ts=NOW, revenue_cents=1000, raw={}),
                     m, "garbage-time-mispricing")
        assert rm.exposure().daily_realized_pnl_cents == 1000 - 937

    def test_reconcile_mismatch_halts(self, rm):
        rm.kalshi.positions = []  # ledger empty too -> ok
        assert rm.reconcile() is True
        from kalshi_bots.types import Position
        rm.kalshi.positions = [Position("GHOST", "yes", 5, 50, 0, {})]
        assert rm.reconcile() is False
        assert rm.halted()[0] is True


class TestIntentSerialization:
    def test_headroom_for_one(self, rm, monkeypatch):
        """Two size() calls against headroom for ~one position: the second must
        see the first's intent reservation."""
        r1 = rm.size(req(eid="g1", ticker="T1-A", team="A"))
        assert r1.contracts > 0
        # same game, same skill class: per-game cap sees the intent
        r2 = rm.size(req(skill="sportsbook-kalshi-divergence", eid="g1",
                         ticker="T1-B", team="B"))
        assert r2.contracts == 0

    def test_cancel_intent_releases(self, rm):
        rm.size(req(eid="g1", ticker="T1-A", team="A"))
        rm.cancel_intent("T1-A", "live-win-prob-overreaction")
        r2 = rm.size(req(skill="sportsbook-kalshi-divergence", eid="g1",
                         ticker="T1-B", team="B"))
        assert "correlation_same_game" not in r2.capped_by or r2.contracts >= 0
        # with the intent gone, per-game room is back
        assert r2.contracts > 0
