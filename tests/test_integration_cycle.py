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
        """The trader must treat every lifecycle signal as a non-trade and the
        (sprint-2) candidate path as an explicit decline — no silent paths."""
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
