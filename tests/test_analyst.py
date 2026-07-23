"""analyst agent tests (sprint-2 scope: window-close handling is a safe no-op
until the 15-minute-cadence postmortem lands in sprint-4)."""
from datetime import datetime, timedelta, timezone

from kalshi_bots.agents.analyst import Analyst
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import WindowRef


def make_window():
    closes = datetime(2026, 7, 23, 1, 30, tzinfo=timezone.utc)
    return WindowRef(series_ticker="KXBTC15M",
                     event_ticker="KXBTC15M-26JUL222130",
                     market_ticker="KXBTC15M-26JUL222130-30",
                     opens_at=closes - timedelta(minutes=15), closes_at=closes)


class TestWindowClose:
    def test_on_window_close_is_safe(self, tmp_path):
        root = tmp_path / "vault"
        (root / "04-trade-history/postmortems").mkdir(parents=True)
        analyst = Analyst(Vault(root=str(root)), broker=object())
        assert analyst.on_window_close(make_window()) is None
