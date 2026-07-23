"""Streaming-loop integration test with synthetic components (sprint-2 scope):
window resolution -> lifecycle signals -> vault window notes -> routing through
the orchestrator tick. The full trade path (fair value -> sizing -> paper fill
-> postmortem -> skill stats) is exercised end-to-end from sprint-5, once every
stage exists.

All offline: the resolver is a fake; feed/book are absent (the monitor and
trader must behave with them missing — fail closed, never crash).
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bots.agents.trader import Trader
from kalshi_bots.agents.window_monitor import WindowMonitor
from kalshi_bots.skills.vault import Vault
from kalshi_bots.skills.window_monitor import active_window

UTC = timezone.utc
T_OPEN = datetime(2026, 7, 23, 1, 15, tzinfo=UTC)     # 0115-0130 window opens


class FakeResolver:
    """Deterministic resolve_active: every constructed window verifies, with a
    strike appearing immediately (the real one REST-verifies)."""

    def __init__(self, series="KXBTC15M", strike=66010.86):
        self.series = series
        self.strike = strike

    def resolve_active(self, now):
        w = active_window(now, self.series)
        w.strike = self.strike
        return w


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    for d in ("02-trading-skills", "03-market-context/active-windows",
              "04-trade-history/trades", "04-trade-history/postmortems"):
        (root / d).mkdir(parents=True)
    return Vault(root=str(root))


def run(coro):
    return asyncio.run(coro)


class TestWindowLifecycle:
    def test_open_phase_close_sequence(self, vault):
        monitor = WindowMonitor(vault, FakeResolver(), book=None, feed=None)

        sigs = run(monitor.tick(T_OPEN + timedelta(seconds=5)))
        assert [s.signal_type for s in sigs] == ["window-open"]
        assert sigs[0].phase == "opening"
        assert sigs[0].payload["strike"] == 66010.86

        sigs = run(monitor.tick(T_OPEN + timedelta(minutes=3)))
        assert [s.signal_type for s in sigs] == ["phase-change"]
        assert sigs[0].phase == "midpoint"

        sigs = run(monitor.tick(T_OPEN + timedelta(minutes=13)))
        assert [s.signal_type for s in sigs] == ["phase-change"]
        assert sigs[0].phase == "near_close"

        # crossing the boundary: old window closes, next window opens
        sigs = run(monitor.tick(T_OPEN + timedelta(minutes=15, seconds=1)))
        kinds = [s.signal_type for s in sigs]
        assert kinds == ["window-close", "window-open"]
        assert sigs[0].market_ticker == "KXBTC15M-26JUL222130-30"
        assert sigs[1].market_ticker == "KXBTC15M-26JUL222145-45"

    def test_window_note_written_with_machine_state(self, vault):
        monitor = WindowMonitor(vault, FakeResolver(), book=None, feed=None)
        run(monitor.tick(T_OPEN + timedelta(seconds=5)))
        note = vault.read_note(
            "03-market-context/active-windows/KXBTC15M-26JUL222130-30.md")
        assert note.frontmatter["phase"] == "opening"
        assert note.frontmatter["strike"] == 66010.86
        assert note.frontmatter["event_id"] == "KXBTC15M-26JUL222130"
        assert "- SIGNAL " in note.body  # signal log for postmortem replay

    def test_unresolved_window_watched_never_signaled(self, vault):
        class NoneResolver:
            def resolve_active(self, now):
                return None

        monitor = WindowMonitor(vault, NoneResolver(), book=None, feed=None)
        assert run(monitor.tick(T_OPEN + timedelta(seconds=5))) == []


class TestSignalRouting:
    def test_lifecycle_signals_route_safely_through_trader(self, vault):
        """The trader must treat every lifecycle signal as a non-trade — no
        silent paths."""
        class NullRisk:
            def halted(self):
                return False, None

        class NullDiscord:
            def notify(self, *a, **k):
                pass

        monitor = WindowMonitor(vault, FakeResolver(), book=None, feed=None)
        trader = Trader(vault, broker=None, risk=NullRisk(), discord=NullDiscord())
        sigs = run(monitor.tick(T_OPEN + timedelta(seconds=5)))
        dispositions = [trader.handle_signal(s) for s in sigs]
        assert dispositions == ["not-a-trade-signal"]


class FakeFeed:
    def __init__(self, mid=66000.0, sigma=0.6):
        self.mid, self.sigma = mid, sigma

    def current_composite(self):
        from kalshi_bots.types import CompositeSpot
        if self.mid is None:
            return None
        return CompositeSpot(mid=self.mid, bid=self.mid - 0.5, ask=self.mid + 0.5,
                             source_ts={}, computed_at=datetime.now(UTC),
                             constituents_healthy=5, constituent_count=5)

    def realized_vol(self, window_s=900):
        return self.sigma


class FakeBook:
    """snapshot-only fake; subscribe/unsubscribe are async no-ops."""

    def __init__(self, yes_ask=55, no_ask=55):
        from kalshi_bots.types import DepthLevel, MarketRef, OrderbookSnapshot
        m = MarketRef(family="crypto", series_ticker="KXBTC15M",
                      event_ticker="E", market_ticker="T", yes_label="up",
                      title="", close_ts=None, settlement_notes=None)
        self.snap = OrderbookSnapshot(
            market=m, yes_bid=100 - no_ask, yes_ask=yes_ask,
            no_bid=100 - yes_ask, no_ask=no_ask,
            yes_book=[DepthLevel(yes_ask, 500)], no_book=[DepthLevel(no_ask, 500)],
            devigged_yes_prob=0.5, spread_cents=0, fetched_at=datetime.now(UTC))

    def snapshot(self, ticker):
        return self.snap

    async def subscribe(self, market):
        pass

    async def unsubscribe(self, ticker):
        pass


class TestCandidateEmission:
    """The monitor flags fair-value candidates in entry phases when the model
    diverges from the book; the flag carries the evidence but the trader
    re-verifies everything."""

    MID = T_OPEN + timedelta(minutes=3)  # midpoint phase

    def make_monitor(self, vault, feed=None, book=None, strike=65900.0):
        return WindowMonitor(vault, FakeResolver(strike=strike),
                             book=book or FakeBook(), feed=feed or FakeFeed())

    def test_candidate_emitted_at_midpoint_with_edge(self, vault):
        monitor = self.make_monitor(vault)
        run(monitor.tick(T_OPEN + timedelta(seconds=5)))   # window-open
        sigs = run(monitor.tick(self.MID))
        kinds = [s.signal_type for s in sigs]
        assert "phase-change" in kinds and "fair-value-candidate" in kinds
        cand = next(s for s in sigs if s.signal_type == "fair-value-candidate")
        # spot 66000 over strike 65900 -> model ~72c vs 55c ask -> yes side
        assert cand.payload["side"] == "yes"
        assert cand.payload["edge_cents"] >= 4
        assert cand.payload["entry_price_cents"] == 55

    def test_no_candidate_in_opening_phase(self, vault):
        monitor = self.make_monitor(vault)
        sigs = run(monitor.tick(T_OPEN + timedelta(seconds=30)))
        assert all(s.signal_type != "fair-value-candidate" for s in sigs)

    def test_no_candidate_without_edge(self, vault):
        # book already at fair (~72c): no divergence, no flag
        monitor = self.make_monitor(vault, book=FakeBook(yes_ask=72, no_ask=30))
        run(monitor.tick(T_OPEN + timedelta(seconds=5)))
        sigs = run(monitor.tick(self.MID))
        assert all(s.signal_type != "fair-value-candidate" for s in sigs)

    def test_no_candidate_when_feed_unhealthy(self, vault):
        monitor = self.make_monitor(vault, feed=FakeFeed(mid=None))
        run(monitor.tick(T_OPEN + timedelta(seconds=5)))
        sigs = run(monitor.tick(self.MID))
        assert all(s.signal_type != "fair-value-candidate" for s in sigs)

    def test_cooldown_throttles_repeat_flags(self, vault, monkeypatch):
        import kalshi_bots.agents.window_monitor as wm
        clock = {"t": 1000.0}
        monkeypatch.setattr(wm.time, "monotonic", lambda: clock["t"])
        monitor = self.make_monitor(vault)
        run(monitor.tick(T_OPEN + timedelta(seconds=5)))
        first = run(monitor.tick(self.MID))
        assert any(s.signal_type == "fair-value-candidate" for s in first)
        clock["t"] += 1.0
        again = run(monitor.tick(self.MID + timedelta(seconds=1)))
        assert all(s.signal_type != "fair-value-candidate" for s in again)
        clock["t"] += wm.CANDIDATE_COOLDOWN_S
        later = run(monitor.tick(self.MID + timedelta(seconds=40)))
        assert any(s.signal_type == "fair-value-candidate" for s in later)
