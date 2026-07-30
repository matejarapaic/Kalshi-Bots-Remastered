"""trader agent tests: fresh-data re-verification, full entry path, exit
rules, restart recovery. All offline against in-memory fakes (never mocked
network), per testing conventions.

Model numbers used below (hand-checked): spot 66000, strike 65900, sigma 0.6,
600s remaining -> p_up = Phi(ln(66000/65900) / (0.6*sqrt(600/31536000)))
≈ 0.7188, i.e. fair ~71.9c.
"""
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bots.agents.trader import Trader
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import (
    ApprovalOutcome, CompositeSpot, CryptoSignal, DepthLevel, Fill, MarketRef,
    OrderbookSnapshot, OrderResult, Settlement, SizingResult, WindowRef,
)

NOW = datetime(2026, 7, 23, 1, 20, tzinfo=timezone.utc)
TICKER = "KXBTC15M-26JUL222130-30"


def window(closes_in_s=600.0, strike=65900.0):
    closes = NOW + timedelta(seconds=closes_in_s)
    return WindowRef(series_ticker="KXBTC15M",
                     event_ticker="KXBTC15M-26JUL222130", market_ticker=TICKER,
                     opens_at=closes - timedelta(seconds=900), closes_at=closes,
                     strike=strike)


def market():
    return MarketRef(family="crypto", series_ticker="KXBTC15M",
                     event_ticker="KXBTC15M-26JUL222130", market_ticker=TICKER,
                     yes_label="up", title="", close_ts=None,
                     settlement_notes=None)


def snapshot(yes_ask=55, no_ask=55, depth=500):
    return OrderbookSnapshot(
        market=market(),
        yes_bid=100 - no_ask, yes_ask=yes_ask,
        no_bid=100 - yes_ask, no_ask=no_ask,
        yes_book=[DepthLevel(yes_ask, depth)] if yes_ask else [],
        no_book=[DepthLevel(no_ask, depth)] if no_ask else [],
        devigged_yes_prob=0.5, spread_cents=yes_ask - (100 - no_ask),
        fetched_at=NOW)


def signal(sig_type="fair-value-candidate", ticker=TICKER, w=None):
    return CryptoSignal(signal_type=sig_type, series_ticker="KXBTC15M",
                        market_ticker=ticker, window=w or window(),
                        phase="midpoint",
                        payload={"id": "sig-1", "sigma": 0.6},
                        emitted_at=datetime.now(timezone.utc))


class FakeFeed:
    def __init__(self, mid=66000.0, sigma=0.6, healthy=5, move_pct=None):
        self.mid, self.sigma, self.healthy = mid, sigma, healthy
        self.move_pct = move_pct

    def current_composite(self):
        if self.mid is None:
            return None
        return CompositeSpot(mid=self.mid, bid=self.mid - 0.5, ask=self.mid + 0.5,
                             source_ts={}, computed_at=datetime.now(timezone.utc),
                             constituents_healthy=self.healthy, constituent_count=5)

    def realized_vol(self, window_s=900):
        return self.sigma

    def recent_move_pct(self, window_s=60):
        return self.move_pct


class FakeBook:
    def __init__(self, snap):
        self.snap = snap

    def snapshot(self, ticker):
        return self.snap


class FakeBroker:
    def __init__(self):
        self.orders = []
        self.fill_price_bump = 0
        self.snap = snapshot()

    def get_market(self, ticker, family=""):
        return market()

    def get_orderbook(self, m):
        return self.snap

    def place_order(self, req):
        self.orders.append(req)
        oid = f"o{len(self.orders)}"
        return OrderResult(order_id=oid, status="filled",
                           filled_contracts=req.contracts,
                           avg_fill_price=req.limit_price + self.fill_price_bump,
                           fee_cents=3, raw={"order_id": oid, "status": "filled"})

    def get_fills(self, ticker):
        if not self.orders:
            return []
        req = self.orders[-1]
        oid = f"o{len(self.orders)}"
        return [Fill(order_id=oid, market_ticker=ticker,
                     side=req.side, action=req.action, contracts=req.contracts,
                     price=req.limit_price, taker_fee_cents=3,
                     ts=datetime.now(timezone.utc),
                     raw={"order_id": oid, "count": req.contracts})]


class FakeRisk:
    def __init__(self, contracts=10, is_halted=False):
        self.contracts = contracts
        self.is_halted = is_halted
        self.fills, self.exits, self.cancels = [], [], []

    def halted(self):
        return self.is_halted, "manual" if self.is_halted else None

    def size(self, req):
        return SizingResult(contracts=self.contracts, limit_price=req.entry_price,
                            kelly_fraction_used=0.05, capped_by=[],
                            est_fee_cents_total=7)

    def on_fill(self, fill, market, skill, event_id=""):
        self.fills.append((market.market_ticker, skill))

    def on_exit(self, fill, market, skill):
        self.exits.append(market.market_ticker)

    def cancel_intent(self, ticker, skill):
        self.cancels.append((ticker, skill))


class FakeDiscord:
    def __init__(self, decision="approved"):
        self.decision = decision
        self.cards, self.notes = [], []

    def send_trade_card(self, card):
        self.cards.append(card)
        return ApprovalOutcome(decision=self.decision, decided_by="auto",
                               decided_at=datetime.now(timezone.utc),
                               card_message_id="m1")

    def notify(self, msg, level="info"):
        self.notes.append(msg)


SKILL_FM = {
    "skill": "btc-15min-fair-value", "families": ["KXBTC15M"],
    "signal_types": ["fair-value-candidate"],
    "market_conditions": ["live", "midpoint"],
    "confidence_threshold": 0.6, "risk_profile": "medium",
    "win_rate": None, "sample_size": 0, "status": "draft",
    "last_updated": "2026-07-22",
}


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    for d in ("04-trade-history/trades", "02-trading-skills"):
        (root / d).mkdir(parents=True)
    v = Vault(root=str(root))
    v.write_note("02-trading-skills/btc-15min-fair-value.md", dict(SKILL_FM),
                 "# skill", caller="admin")
    return v


def make_trader(vault, broker=None, risk=None, discord=None, feed=None, book=None):
    return Trader(vault, broker or FakeBroker(), risk or FakeRisk(),
                  discord or FakeDiscord(), env="demo",
                  feed=feed if feed is not None else FakeFeed(),
                  book=book if book is not None else FakeBook(snapshot()))


class TestSignalGates:
    def test_lifecycle_signals_are_not_trades(self, vault):
        t = make_trader(vault)
        for st in ("window-open", "phase-change", "window-close"):
            assert t.handle_signal(signal(st)) == "not-a-trade-signal"

    def test_unresolved_window_declined(self, vault):
        assert make_trader(vault).handle_signal(
            signal(ticker=None)) == "declined:unresolved_window"

    def test_halted_declined(self, vault):
        t = make_trader(vault, risk=FakeRisk(is_halted=True))
        assert t.handle_signal(signal()).startswith("declined:halted")

    def test_no_strike_declined(self, vault):
        assert make_trader(vault).handle_signal(
            signal(w=window(strike=None))) == "declined:no_strike"

    def test_stale_spot_refuses(self, vault):
        t = make_trader(vault, feed=FakeFeed(mid=None))
        assert t.handle_signal(signal()) == "declined:spot_unavailable"

    def test_missing_sigma_refuses(self, vault):
        t = make_trader(vault, feed=FakeFeed(sigma=None))
        assert t.handle_signal(signal()) == "declined:sigma_unavailable"

    def test_draft_skill_matches_on_demo_only(self, vault):
        # env=demo allows draft (calibration); a prod-configured matcher must not
        t = Trader(vault, FakeBroker(), FakeRisk(), FakeDiscord(), env="prod",
                   feed=FakeFeed(), book=FakeBook(snapshot()))
        assert t.handle_signal(signal()) == "declined:matcher_below_threshold"


class TestEntryPath:
    def test_healthy_path_trades(self, vault):
        broker, risk, discord = FakeBroker(), FakeRisk(), FakeDiscord()
        t = make_trader(vault, broker=broker, risk=risk, discord=discord)
        result = t.handle_signal(signal(), now=NOW)
        assert result == "traded:10x@55"
        assert broker.orders[0].side == "yes"        # model ~71.9c vs 55c ask
        assert broker.orders[0].limit_price == 55
        assert risk.fills == [(TICKER, "btc-15min-fair-value")]
        assert len(t.open_trades) == 1
        trade = next(iter(t.open_trades.values()))
        assert trade["window"].strike == 65900.0
        note = vault.read_note(trade["note"])
        assert note.frontmatter["status"] == "open"
        assert note.frontmatter["strike"] == 65900.0
        assert note.frontmatter["entry_conditions"]["edge_ge_min"] is True
        # Kalshi-sourced fields, verbatim from the order/fill objects already
        # fetched at entry — not recomputed, not dropped.
        assert note.frontmatter["kalshi_order_id"] == "o1"
        assert note.frontmatter["order_status"] == "filled"
        assert note.frontmatter["entry_order_raw"] == {"order_id": "o1", "status": "filled"}
        assert note.frontmatter["entry_fills_raw"] == [{"order_id": "o1", "count": 10}]
        assert note.frontmatter["fill_ts"] is not None

    def test_small_edge_declined(self, vault):
        # ask 70 vs model ~71.9 -> edge ~1.9 < 4
        t = make_trader(vault, book=FakeBook(snapshot(yes_ask=70, no_ask=32)))
        r = t.handle_signal(signal(), now=NOW)
        assert r.startswith("declined:entry_verification_failed")
        assert "edge_ge_min" in r

    def test_near_close_phase_declined(self, vault):
        w = window(closes_in_s=100.0)  # inside near_close
        t = make_trader(vault)
        r = t.handle_signal(signal(w=w), now=NOW)
        assert "phase_allowed" in r

    def test_thin_book_declined(self, vault):
        t = make_trader(vault, book=FakeBook(snapshot(depth=50)))
        r = t.handle_signal(signal(), now=NOW)
        assert "depth_both_sides" in r

    def test_implausible_sigma_declined(self, vault):
        t = make_trader(vault, feed=FakeFeed(sigma=3.0))
        r = t.handle_signal(signal(), now=NOW)
        assert "sigma_plausible" in r

    def test_rejection_cancels_intent(self, vault):
        risk = FakeRisk()
        t = make_trader(vault, risk=risk, discord=FakeDiscord(decision="rejected"))
        r = t.handle_signal(signal(), now=NOW)
        assert r == "declined:approval_rejected"
        assert risk.cancels == [(TICKER, "btc-15min-fair-value")]

    def test_one_position_per_market_per_skill(self, vault):
        t = make_trader(vault)
        assert t.handle_signal(signal(), now=NOW).startswith("traded")
        assert t.handle_signal(signal(), now=NOW) == "declined:position_exists"

    def test_no_feed_fails_closed(self, vault):
        t = Trader(vault, FakeBroker(), FakeRisk(), FakeDiscord(), env="demo",
                   feed=None, book=FakeBook(snapshot()))
        assert t.handle_signal(signal()) == "declined:no_feed"


class TestExitRules:
    def open_position(self, vault, **trader_kw):
        t = make_trader(vault, **trader_kw)
        assert t.handle_signal(signal(), now=NOW).startswith("traded")
        return t

    def exit_reason(self, t, now=NOW):
        (coid, trade), = t.open_trades.items()
        return t._exit_reason(trade, now)

    def test_holds_while_edge_persists(self, vault):
        t = self.open_position(vault)
        assert self.exit_reason(t) is None

    def test_edge_converged_take_profit(self, vault):
        t = self.open_position(vault)
        # market moved to fair: ask 71 vs model ~71.9 -> edge ~0.9 <= 1
        t.book = FakeBook(snapshot(yes_ask=71, no_ask=31))
        assert self.exit_reason(t) == "edge_converged"

    def test_edge_inverted_stop(self, vault):
        t = self.open_position(vault)
        # market overshot fair: NO side now cheap (no_ask 25 vs model_down ~28)
        t.book = FakeBook(snapshot(yes_ask=76, no_ask=25))
        assert self.exit_reason(t) == "edge_inverted"

    def test_depth_collapse_exits(self, vault):
        t = self.open_position(vault)
        t.book = FakeBook(snapshot(depth=40))  # < 100 * 0.5
        assert self.exit_reason(t) == "depth_collapse"

    def test_feed_loss_exits(self, vault):
        t = self.open_position(vault)
        t.feed = FakeFeed(mid=None)
        assert self.exit_reason(t) == "feed_loss"

    def test_near_close_overrides_everything(self, vault):
        t = self.open_position(vault)
        t.feed = FakeFeed(mid=None)  # would be feed_loss, but near_close first
        near = NOW + timedelta(seconds=500)  # window closes at NOW+600
        assert self.exit_reason(t, now=near) == "near_close_exit"

    def test_stop_loss_exits_on_large_unrealized_loss(self, vault):
        t = self.open_position(vault)
        # entered yes @ 55c; book now shows yes_bid collapsed to 19c, a ~65%
        # unrealized loss vs entry -- past the 50% STOP_LOSS_PCT backstop
        t.book = FakeBook(snapshot(yes_ask=81, no_ask=81))
        assert self.exit_reason(t) == "stop_loss"

    def test_stop_loss_is_universal_even_for_non_fair_value_skills(self, vault):
        # a skill with no thesis-invalidation rules of its own (only
        # near_close/stop_loss apply) still gets the stop-loss backstop
        t = self.open_position(vault)
        (coid, trade), = t.open_trades.items()
        trade["skill"] = "btc-15min-vol-spike"
        t.book = FakeBook(snapshot(yes_ask=81, no_ask=81))
        assert t._exit_reason(trade, NOW) == "stop_loss"

    def test_no_stop_loss_within_threshold(self, vault):
        t = self.open_position(vault)
        # entered yes @ 55c; bid at 30c is only a ~45% loss -- under 50%
        t.book = FakeBook(snapshot(yes_ask=70, no_ask=70))
        assert self.exit_reason(t) is None

    def test_manage_positions_executes_exit(self, vault):
        broker = FakeBroker()
        risk = FakeRisk()
        t = self.open_position(vault, broker=broker, risk=risk)
        (_, trade), = t.open_trades.items()
        note_path = trade["note"]
        t.book = FakeBook(snapshot(yes_ask=71, no_ask=31))
        broker.snap = snapshot(yes_ask=71, no_ask=31)
        actions = t.manage_positions(now=NOW)
        assert len(actions) == 1 and actions[0].endswith(":edge_converged")
        assert broker.orders[-1].action == "sell"
        assert risk.exits == [TICKER]
        assert t.open_trades == {}
        # exit order/fill data — the same "already fetched, don't discard it"
        # fields as the entry note, using the exit order's own id/status/raw.
        note = vault.read_note(note_path)
        # FakeBroker.orders stores the OrderRequest, not the OrderResult;
        # its order_id is assigned as f"o{len(self.orders)}" (see FakeBroker
        # .place_order) — the exit is the 2nd order placed (entry + exit).
        exit_order_id = f"o{len(broker.orders)}"
        assert note.frontmatter["exit_order_id"] == exit_order_id
        assert note.frontmatter["exit_status"] == "filled"
        assert note.frontmatter["exit_order_raw"] == {
            "order_id": exit_order_id, "status": "filled"}
        assert note.frontmatter["exit_fills_raw"] == [
            {"order_id": exit_order_id, "count": trade["contracts"]}]
        assert note.frontmatter["exit_fill_ts"] is not None


class TestLedgerFromOrderResult:
    """Regression from the 2026-07-29 live session: the exposure ledger was
    updated from broker.get_fills(), which indexes asynchronously and often
    returns nothing right after placement — so on_fill/on_exit silently
    never ran, positions went stale, and every exit pnl fell back to the
    fee-blind estimate. The ledger must be booked from the OrderResult the
    trader already holds."""

    class _LaggingBroker(FakeBroker):
        def get_fills(self, ticker):
            return []  # feed hasn't indexed the fill yet

    class _CapturingRisk(FakeRisk):
        def __init__(self):
            super().__init__()
            self.fill_objs, self.exit_objs = [], []

        def on_fill(self, fill, market, skill, event_id=""):
            self.fill_objs.append(fill)
            super().on_fill(fill, market, skill, event_id)

        def on_exit(self, fill, market, skill):
            self.exit_objs.append(fill)
            super().on_exit(fill, market, skill)
            return 40

    def test_entry_books_ledger_even_when_fills_feed_lags(self, vault):
        broker, risk = self._LaggingBroker(), self._CapturingRisk()
        t = make_trader(vault, broker=broker, risk=risk)
        assert t.handle_signal(signal(), now=NOW).startswith("traded")
        (f,) = risk.fill_objs
        assert f.contracts == 10 and f.price == 55  # from the OrderResult
        assert f.side == "yes" and f.taker_fee_cents == 3

    def test_exit_books_ledger_even_when_fills_feed_lags(self, vault):
        broker, risk = self._LaggingBroker(), self._CapturingRisk()
        t = make_trader(vault, broker=broker, risk=risk)
        assert t.handle_signal(signal(), now=NOW).startswith("traded")
        (_, trade), = t.open_trades.items()
        note_path = trade["note"]
        t.book = FakeBook(snapshot(yes_ask=71, no_ask=31))
        broker.snap = snapshot(yes_ask=71, no_ask=31)
        actions = t.manage_positions(now=NOW)
        assert len(actions) == 1
        (f,) = risk.exit_objs
        assert f.side == "yes" and f.action == "sell"
        assert f.contracts == 10
        # ledger pnl (fee-inclusive) lands in the note, not the fee-blind
        # fallback estimate
        note = vault.read_note(note_path)
        assert note.frontmatter["realized_pnl_cents"] == 40


def write_open_trade(vault, ticker=TICKER, coid="kb-abc123"):
    vault.write_note(f"04-trade-history/trades/2026-07-23-{coid}.md", {
        "client_order_id": coid, "event_id": "KXBTC15M-26JUL222130",
        "family": "KXBTC15M", "market_ticker": ticker,
        "skill": "btc-15min-fair-value", "side": "yes", "contracts": 10,
        "entry_price_cents": 52, "signal_price_cents": 52, "fee_cents": 5,
        "model_prob": 0.6, "strike": 65900.0, "entry_conditions": {},
        "signal_id": "s1", "status": "open", "realized_pnl_cents": None,
        "exit_deviation": False, "env": "demo", "opened_at": NOW.isoformat(),
    }, "trade", caller="trader")


class TestRestartRecovery:
    def test_restores_open_trades_with_window_and_strike(self, vault):
        write_open_trade(vault)
        t = make_trader(vault)
        counts = t.reload_open_trades()
        assert counts == {"restored": 1, "closed": 0}
        trade = t.open_trades["kb-abc123"]
        assert trade["window"].closes_at == datetime(2026, 7, 23, 1, 30,
                                                     tzinfo=timezone.utc)
        assert trade["window"].strike == 65900.0  # edge exits work post-restart

    def test_closes_settled_while_down(self, vault):
        write_open_trade(vault)
        t = make_trader(vault)
        settled = {TICKER: Settlement(
            market_ticker=TICKER, result="yes", settled_ts=NOW,
            revenue_cents=1000, raw={"ticker": TICKER, "market_result": "yes"},
            event_ticker="KXBTC15M-26JUL222130", yes_count=10.0, no_count=0.0,
            fee_cents=5, total_cost_cents=525)}
        counts = t.reload_open_trades(settled)
        assert counts == {"restored": 0, "closed": 1}
        note = vault.read_note("04-trade-history/trades/2026-07-23-kb-abc123.md")
        assert note.frontmatter["status"] == "closed"
        # 10*100 - (10*52 + 5) = 475 on the recorded basis
        assert note.frontmatter["realized_pnl_cents"] == 475
        # Kalshi's own settlement record, carried alongside the internally
        # computed P&L above (not in place of it) so the two can be
        # cross-checked against each other.
        assert note.frontmatter["settlement_raw"] == {
            "ticker": TICKER, "market_result": "yes"}
        assert note.frontmatter["settlement_revenue_cents"] == 1000
        assert note.frontmatter["settlement_yes_count"] == 10.0
        assert note.frontmatter["settlement_no_count"] == 0.0
        assert note.frontmatter["settlement_total_cost_cents"] == 525
