"""analyst agent tests: settlement polling, paper settlement, Discord rollup
batching (the single biggest UX difference from per-event postmortems — get
this wrong and the operator mutes the channel)."""
from datetime import datetime, timedelta, timezone

import pytest

import kalshi_bots.agents.analyst as analyst_mod
from kalshi_bots.agents.analyst import ROLLUP_WINDOWS, Analyst
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import WindowRef

UTC = timezone.utc
CLOSES = datetime(2026, 7, 23, 1, 30, tzinfo=UTC)

SKILL_FM = {
    "skill": "btc-15min-fair-value", "families": ["KXBTC15M"],
    "signal_types": ["fair-value-candidate"],
    "market_conditions": ["live", "midpoint"], "confidence_threshold": 0.6,
    "risk_profile": "medium", "win_rate": None, "sample_size": 0,
    "status": "draft", "last_updated": "2026-07-22",
}


def make_window(i=0):
    closes = CLOSES + timedelta(minutes=15 * i)
    from kalshi_bots.skills.window_monitor import market_ticker_for_close
    ticker = market_ticker_for_close(closes)
    return WindowRef(series_ticker="KXBTC15M",
                     event_ticker=ticker.rsplit("-", 1)[0],
                     market_ticker=ticker,
                     opens_at=closes - timedelta(minutes=15), closes_at=closes,
                     strike=65900.0)


class FakeBroker:
    """get_market_raw with programmable finalization."""

    def __init__(self):
        self.markets: dict[str, dict] = {}
        self.settled_calls = []

    def finalize(self, ticker, result="yes", expiration=66100.0):
        self.markets[ticker] = {"ticker": ticker, "status": "finalized",
                                "result": result,
                                "expiration_value": expiration}

    def get_market_raw(self, ticker):
        return self.markets.get(ticker, {"ticker": ticker, "status": "closed",
                                         "result": ""})

    def settle(self, ticker, result):  # paper-broker interface
        self.settled_calls.append((ticker, result))


class FakeDiscord:
    def __init__(self):
        self.notes = []

    def notify(self, msg, level="info"):
        self.notes.append(msg)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    for d in ("02-trading-skills", "03-market-context/active-windows",
              "04-trade-history/trades", "04-trade-history/postmortems"):
        (root / d).mkdir(parents=True)
    v = Vault(root=str(root))
    v.write_note("02-trading-skills/btc-15min-fair-value.md", dict(SKILL_FM),
                 "# skill", caller="admin")
    return v


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 1000.0}
    monkeypatch.setattr(analyst_mod.time, "monotonic", lambda: state["t"])
    return state


def make_analyst(vault, broker=None, discord=None, paper=None):
    return Analyst(vault, broker or FakeBroker(), discord=discord or FakeDiscord(),
                   env="demo", paper_broker=paper)


class TestSettlementPolling:
    def test_polls_until_finalized_then_postmortems(self, vault, clock):
        broker, discord = FakeBroker(), FakeDiscord()
        a = make_analyst(vault, broker=broker, discord=discord)
        w = make_window()
        a.on_window_close(w)
        clock["t"] += 3
        assert a.poll_pending() == []          # not finalized yet
        broker.finalize(w.market_ticker, "yes")
        clock["t"] += 1
        assert a.poll_pending() == []          # inside per-window throttle
        clock["t"] += 6
        reports = a.poll_pending()
        assert len(reports) == 1
        assert reports[0].settlement_status == "settled"
        assert w.event_ticker not in a._pending

    def test_paper_broker_settles_from_market_result(self, vault, clock):
        broker = FakeBroker()
        a = make_analyst(vault, broker=broker, paper=broker)
        w = make_window()
        a.on_window_close(w)
        broker.finalize(w.market_ticker, "no", expiration=65000.0)
        clock["t"] += 3
        a.poll_pending()
        assert broker.settled_calls == [(w.market_ticker, "no")]

    def test_settled_direction_written_to_window_note(self, vault, clock):
        broker = FakeBroker()
        a = make_analyst(vault, broker=broker)
        w = make_window()
        vault.write_note(f"03-market-context/active-windows/{w.market_ticker}.md",
                         {"event_id": w.event_ticker, "strike": 65900.0},
                         "# w\n", caller="window-monitor")
        a.on_window_close(w)
        broker.finalize(w.market_ticker, "yes", expiration=66100.0)
        clock["t"] += 3
        a.poll_pending()
        fm = vault.read_note(
            f"03-market-context/active-windows/{w.market_ticker}.md").frontmatter
        assert fm["settled_direction"] == "up"
        assert fm["expiration_value"] == 66100.0

    def test_gives_up_after_timeout_reports_pending(self, vault, clock):
        discord = FakeDiscord()
        a = make_analyst(vault, discord=discord)
        w = make_window()
        a.on_window_close(w)
        clock["t"] += analyst_mod.SETTLE_GIVE_UP_S + 10
        reports = a.poll_pending()
        assert len(reports) == 1
        assert reports[0].settlement_status == "pending"
        assert any("never finalized" in n for n in discord.notes)
        assert a._pending == {}


class TestRollupBatching:
    def settle_n(self, a, broker, clock, n, start=0):
        reports = []
        for i in range(start, start + n):
            w = make_window(i)
            a.on_window_close(w)
            broker.finalize(w.market_ticker, "yes")
            clock["t"] += 6
            reports += a.poll_pending()
        return reports

    def test_quiet_windows_batch_into_one_rollup(self, vault, clock):
        broker, discord = FakeBroker(), FakeDiscord()
        a = make_analyst(vault, broker=broker, discord=discord)
        self.settle_n(a, broker, clock, ROLLUP_WINDOWS - 1)
        assert discord.notes == []             # quiet windows stay quiet
        self.settle_n(a, broker, clock, 1, start=ROLLUP_WINDOWS - 1)
        rollups = [n for n in discord.notes if n.startswith("ROLLUP")]
        assert len(rollups) == 1
        assert f"ROLLUP {ROLLUP_WINDOWS} windows" in rollups[0]

    def test_traded_window_notifies_immediately(self, vault, clock):
        broker, discord = FakeBroker(), FakeDiscord()
        a = make_analyst(vault, broker=broker, discord=discord)
        w = make_window()
        vault.write_note("04-trade-history/trades/2026-07-23-t1.md", {
            "client_order_id": "t1", "event_id": w.event_ticker,
            "family": "KXBTC15M", "market_ticker": w.market_ticker,
            "skill": "btc-15min-fair-value", "side": "yes", "contracts": 10,
            "entry_price_cents": 55, "signal_price_cents": 55, "fee_cents": 5,
            "model_prob": 0.72, "sigma": 0.6, "spot": 66000.0, "strike": 65900.0,
            "entry_conditions": {}, "realized_pnl_cents": 100,
            "status": "closed", "exit_deviation": False, "env": "demo",
            "signal_id": "s1",
        }, "trade", caller="trader")
        a.on_window_close(w)
        broker.finalize(w.market_ticker, "yes")
        clock["t"] += 6
        a.poll_pending()
        assert any(n.startswith("POSTMORTEM") for n in discord.notes)

    def test_stats_flush_happens_per_batch_not_per_window(self, vault, clock):
        broker = FakeBroker()
        a = make_analyst(vault, broker=broker)
        # 3 settled windows with one traded outcome each -> no stats yet
        for i in range(ROLLUP_WINDOWS - 1):
            w = make_window(i)
            vault.write_note(f"04-trade-history/trades/2026-07-23-t{i}.md", {
                "client_order_id": f"t{i}", "event_id": w.event_ticker,
                "family": "KXBTC15M", "market_ticker": w.market_ticker,
                "skill": "btc-15min-fair-value", "side": "yes", "contracts": 10,
                "entry_price_cents": 55, "signal_price_cents": 55, "fee_cents": 5,
                "model_prob": 0.72, "sigma": 0.6, "spot": 66000.0,
                "strike": 65900.0, "entry_conditions": {},
                "realized_pnl_cents": 100, "status": "closed",
                "exit_deviation": False, "env": "demo", "signal_id": f"s{i}",
            }, "trade", caller="trader")
            a.on_window_close(w)
            broker.finalize(w.market_ticker, "yes")
            clock["t"] += 6
            a.poll_pending()
        fm = vault.read_note("02-trading-skills/btc-15min-fair-value.md").frontmatter
        assert fm.get("demo_sample_size") in (None, 0)  # batched, not yet flushed
        # 4th window completes the batch -> stats land
        w = make_window(ROLLUP_WINDOWS - 1)
        a.on_window_close(w)
        broker.finalize(w.market_ticker, "yes")
        clock["t"] += 6
        a.poll_pending()
        fm = vault.read_note("02-trading-skills/btc-15min-fair-value.md").frontmatter
        assert fm["demo_sample_size"] == ROLLUP_WINDOWS - 1
