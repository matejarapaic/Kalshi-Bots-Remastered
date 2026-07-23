"""trader agent tests (sprint-2 scope: decline-by-default, restart recovery,
near-close exit sweep). The full entry path (fair-value re-verification,
sizing, execution) is covered from sprint-3.
"""
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bots.agents.trader import Trader
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import (
    CryptoSignal, MarketRef, OrderbookSnapshot, OrderResult, Settlement,
    WindowRef,
)

NOW = datetime(2026, 7, 23, 1, 20, tzinfo=timezone.utc)
TICKER = "KXBTC15M-26JUL222130-30"


def window(closes_at=None):
    closes = closes_at or datetime(2026, 7, 23, 1, 30, tzinfo=timezone.utc)
    return WindowRef(series_ticker="KXBTC15M",
                     event_ticker="KXBTC15M-26JUL222130", market_ticker=TICKER,
                     opens_at=closes - timedelta(minutes=15), closes_at=closes,
                     strike=66010.86)


def market():
    return MarketRef(family="crypto", series_ticker="KXBTC15M",
                     event_ticker="KXBTC15M-26JUL222130", market_ticker=TICKER,
                     yes_label="up", title="", close_ts=None,
                     settlement_notes=None)


def signal(sig_type="fair-value-candidate", ticker=TICKER):
    return CryptoSignal(signal_type=sig_type, series_ticker="KXBTC15M",
                        market_ticker=ticker, window=window(), phase="midpoint",
                        payload={"id": "sig-1"}, emitted_at=NOW)


class FakeBroker:
    def __init__(self):
        self.markets = {TICKER: market()}
        self.yes_bid = 55
        self.orders = []
        self.fills = []

    def get_market(self, ticker, family=""):
        return self.markets[ticker]

    def get_orderbook(self, m):
        return OrderbookSnapshot(market=m, yes_bid=self.yes_bid,
                                 yes_ask=(self.yes_bid + 2) if self.yes_bid else None,
                                 no_bid=(100 - self.yes_bid - 2) if self.yes_bid else None,
                                 no_ask=(100 - self.yes_bid) if self.yes_bid else None,
                                 yes_book=[], no_book=[],
                                 devigged_yes_prob=0.56, spread_cents=2,
                                 fetched_at=NOW)

    def place_order(self, req):
        self.orders.append(req)
        return OrderResult(order_id=f"o{len(self.orders)}", status="filled",
                           filled_contracts=req.contracts,
                           avg_fill_price=req.limit_price + 1, fee_cents=3, raw={})

    def get_fills(self, ticker):
        return self.fills


class FakeRisk:
    def __init__(self, is_halted=False):
        self.is_halted = is_halted
        self.exits = []

    def halted(self):
        return self.is_halted, "manual" if self.is_halted else None

    def on_exit(self, fill, market, skill):
        self.exits.append(market.market_ticker)


class FakeDiscord:
    def __init__(self):
        self.notes = []

    def notify(self, msg, level="info"):
        self.notes.append(msg)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    for d in ("04-trade-history/trades", "02-trading-skills"):
        (root / d).mkdir(parents=True)
    return Vault(root=str(root))


def make_trader(vault, broker=None, risk=None):
    return Trader(vault, broker or FakeBroker(), risk or FakeRisk(), FakeDiscord())


class TestSignalHandling:
    def test_lifecycle_signals_are_not_trades(self, vault):
        t = make_trader(vault)
        for st in ("window-open", "phase-change", "window-close"):
            assert t.handle_signal(signal(st)) == "not-a-trade-signal"

    def test_unresolved_window_declined(self, vault):
        t = make_trader(vault)
        assert t.handle_signal(signal(ticker=None)) == "declined:unresolved_window"

    def test_halted_declined(self, vault):
        t = make_trader(vault, risk=FakeRisk(is_halted=True))
        assert t.handle_signal(signal()).startswith("declined:halted")

    def test_candidate_declined_until_model_wired(self, vault):
        t = make_trader(vault)
        assert t.handle_signal(signal()) == "declined:model_not_wired(sprint-3)"


def write_open_trade(vault, ticker=TICKER, coid="kb-abc123"):
    vault.write_note(f"04-trade-history/trades/2026-07-23-{coid}.md", {
        "client_order_id": coid, "event_id": "KXBTC15M-26JUL222130",
        "family": "KXBTC15M", "market_ticker": ticker,
        "skill": "btc-15min-fair-value", "side": "yes", "contracts": 10,
        "entry_price_cents": 52, "signal_price_cents": 52, "fee_cents": 5,
        "model_prob": 0.6, "entry_conditions": {}, "signal_id": "s1",
        "status": "open", "realized_pnl_cents": None, "exit_deviation": False,
        "env": "demo", "opened_at": NOW.isoformat(),
    }, "trade", caller="trader")


class TestRestartRecovery:
    def test_restores_open_trades_with_window(self, vault):
        write_open_trade(vault)
        t = make_trader(vault)
        counts = t.reload_open_trades()
        assert counts == {"restored": 1, "closed": 0}
        trade = t.open_trades["kb-abc123"]
        assert trade["window"] is not None
        assert trade["window"].closes_at == datetime(2026, 7, 23, 1, 30,
                                                     tzinfo=timezone.utc)

    def test_closes_settled_while_down(self, vault):
        write_open_trade(vault)
        t = make_trader(vault)
        settled = {TICKER: Settlement(market_ticker=TICKER, result="yes",
                                      settled_ts=NOW, revenue_cents=0, raw={})}
        counts = t.reload_open_trades(settled)
        assert counts == {"restored": 0, "closed": 1}
        note = vault.read_note("04-trade-history/trades/2026-07-23-kb-abc123.md")
        assert note.frontmatter["status"] == "closed"
        # 10*100 - (10*52 + 5) = 475 on the recorded basis
        assert note.frontmatter["realized_pnl_cents"] == 475


class TestExitSweep:
    def test_near_close_exits_position(self, vault):
        write_open_trade(vault)
        broker = FakeBroker()
        risk = FakeRisk()
        t = make_trader(vault, broker=broker, risk=risk)
        t.reload_open_trades()
        # mid-window: no exit
        assert t.manage_positions(now=NOW) == []
        # 2 minutes before close: near_close -> mechanical exit
        near = datetime(2026, 7, 23, 1, 28, tzinfo=timezone.utc)
        actions = t.manage_positions(now=near)
        assert actions == ["exited:kb-abc123:near_close_exit"]
        assert t.open_trades == {}
        assert broker.orders[0].action == "sell"
        assert broker.orders[0].limit_price == broker.yes_bid - 1
        note = vault.read_note("04-trade-history/trades/2026-07-23-kb-abc123.md")
        assert note.frontmatter["status"] == "closed"
        assert note.frontmatter["exit_reason"] == "near_close_exit"

    def test_no_bid_retries_later(self, vault):
        write_open_trade(vault)
        broker = FakeBroker()
        broker.yes_bid = None
        t = make_trader(vault, broker=broker)
        t.reload_open_trades()
        near = datetime(2026, 7, 23, 1, 28, tzinfo=timezone.utc)
        assert t.manage_positions(now=near) == []
        assert "kb-abc123" in t.open_trades  # kept for retry, never dropped
