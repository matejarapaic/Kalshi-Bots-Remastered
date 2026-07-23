from datetime import datetime, timezone

import pytest

from kalshi_bots.agents.trader import Trader
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import MarketRef, Settlement

NOW = datetime(2026, 7, 18, 18, 0, tzinfo=timezone.utc)


class FakeBroker:
    def __init__(self):
        self.markets = {}  # ticker -> MarketRef

    def get_market(self, ticker, league=""):
        if ticker not in self.markets:
            raise RuntimeError(f"unknown market {ticker}")
        return self.markets[ticker]


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "04-trade-history/trades").mkdir(parents=True)
    return Vault(root=str(root))


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def trader(vault, broker):
    return Trader(vault, broker, matcher=None, risk=None, discord=None, env="demo")


def write_open_trade(v, coid, ticker, side="yes", contracts=5, entry=48, fee=9):
    v.write_note(f"04-trade-history/trades/{coid}.md", {
        "client_order_id": coid, "espn_event_id": "e1", "league": "mlb",
        "market_ticker": ticker, "skill": "sportsbook-kalshi-divergence",
        "side": side, "contracts": contracts, "entry_price_cents": entry,
        "signal_price_cents": entry, "fee_cents": fee, "model_prob": 0.6,
        "entry_conditions": {}, "signal_id": "sig-1", "status": "open",
        "realized_pnl_cents": None, "exit_deviation": False, "env": "demo",
        "opened_at": NOW.isoformat(),
    }, "# trade", caller="trader")


class TestReloadOpenTrades:
    def test_restores_still_live_position(self, trader, broker):
        ticker = "KXMLBGAME-26JUL171905LADNYY-NYY"
        write_open_trade(trader.vault, "kb-1", ticker)
        broker.markets[ticker] = MarketRef(
            league="mlb", series_ticker="KXMLBGAME", event_ticker=ticker,
            market_ticker=ticker, yes_team_kalshi_abbr="NYY", title="",
            close_ts=None, settlement_notes=None)

        counts = trader.reload_open_trades()

        assert counts == {"restored": 1, "closed": 0}
        assert "kb-1" in trader.open_trades
        t = trader.open_trades["kb-1"]
        assert t["market_ticker"] == ticker
        assert t["contracts"] == 5
        assert t["market"].market_ticker == ticker

    def test_closes_note_for_position_settled_while_down(self, trader):
        ticker = "KXMLBGAME-26JUL172005MINCHC-CHC"
        write_open_trade(trader.vault, "kb-2", ticker, side="yes", contracts=7,
                         entry=26, fee=9)
        settled = {ticker: Settlement(market_ticker=ticker, result="no",
                                      settled_ts=NOW, revenue_cents=0, raw={})}

        counts = trader.reload_open_trades(settled)

        assert counts == {"restored": 0, "closed": 1}
        assert "kb-2" not in trader.open_trades
        note = trader.vault.read_note("04-trade-history/trades/kb-2.md")
        assert note.frontmatter["status"] == "closed"
        assert note.frontmatter["exit_reason"] == "settled_while_down"
        assert note.frontmatter["realized_pnl_cents"] == -(7 * 26 + 9)

    def test_closes_note_for_position_settled_win(self, trader):
        ticker = "T-WIN"
        write_open_trade(trader.vault, "kb-3", ticker, side="no", contracts=5,
                         entry=48, fee=9)
        settled = {ticker: Settlement(market_ticker=ticker, result="no",
                                      settled_ts=NOW, revenue_cents=0, raw={})}

        counts = trader.reload_open_trades(settled)

        assert counts == {"restored": 0, "closed": 1}
        note = trader.vault.read_note("04-trade-history/trades/kb-3.md")
        assert note.frontmatter["realized_pnl_cents"] == 5 * 100 - (5 * 48 + 9)

    def test_market_fetch_failure_skips_without_raising(self, trader):
        write_open_trade(trader.vault, "kb-4", "GONE-TICKER")
        counts = trader.reload_open_trades()
        assert counts == {"restored": 0, "closed": 0}
        assert "kb-4" not in trader.open_trades

    def test_ignores_closed_trades(self, trader, broker):
        v = trader.vault
        v.write_note("04-trade-history/trades/kb-5.md", {
            "client_order_id": "kb-5", "espn_event_id": "e1", "league": "mlb",
            "market_ticker": "T-CLOSED", "skill": "sportsbook-kalshi-divergence",
            "side": "yes", "contracts": 5, "entry_price_cents": 48,
            "signal_price_cents": 48, "fee_cents": 9, "model_prob": 0.6,
            "entry_conditions": {}, "signal_id": "sig-1", "status": "closed",
            "realized_pnl_cents": 10, "exit_deviation": False, "env": "demo",
            "opened_at": NOW.isoformat(),
        }, "# trade", caller="trader")

        counts = trader.reload_open_trades()

        assert counts == {"restored": 0, "closed": 0}
