from datetime import date, datetime, timezone

import pytest

from kalshi_bots.agents.analyst import Analyst
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import GameState, TeamRef


def make_game(eid, start_time, league="mlb"):
    return GameState(
        league=league, espn_event_id=eid, status="scheduled",
        home=TeamRef(league, "BOS", None, "Boston Red Sox"),
        away=TeamRef(league, "TB", None, "Tampa Bay Rays"),
        home_score=0, away_score=0, period=0, period_half=None,
        clock_seconds=None, win_prob_home=None, win_prob_source_ts=None,
        start_time=start_time, fetched_at=start_time,
    )


class FakeEspn:
    def __init__(self, games):
        self.games = games

    def get_scoreboard(self, league):
        return self.games


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "03-market-context/daily-slate").mkdir(parents=True)
    return Vault(root=str(root))


class TestNightlySlateEtBoundary:
    def test_late_night_et_game_lands_on_correct_day(self, vault):
        """A 10:40 PM ET start on July 17 is 2026-07-18T02:40:00+00:00 in UTC —
        naive UTC-date comparison would bucket it into July 18, not July 17."""
        late_game = make_game("g1", datetime(2026, 7, 18, 2, 40, tzinfo=timezone.utc))
        analyst = Analyst(vault, kalshi=None, espn=FakeEspn([late_game]))
        analyst.nightly_slate(["mlb"], for_day=date(2026, 7, 17))
        note = vault.read_note("03-market-context/daily-slate/2026-07-17-preview.md")
        assert "TB @ BOS" in note.body
        assert "MLB — 1 games" in note.body

    def test_early_morning_utc_game_excluded_from_previous_et_day(self, vault):
        """The same game must NOT also appear in the July 18 preview."""
        late_game = make_game("g1", datetime(2026, 7, 18, 2, 40, tzinfo=timezone.utc))
        analyst = Analyst(vault, kalshi=None, espn=FakeEspn([late_game]))
        analyst.nightly_slate(["mlb"], for_day=date(2026, 7, 18))
        note = vault.read_note("03-market-context/daily-slate/2026-07-18-preview.md")
        assert "MLB — 0 games" in note.body

    def test_default_day_is_et_tomorrow_not_host_local(self, vault, monkeypatch):
        """for_day=None must derive from the ET calendar day, not date.today()
        (the host machine's local timezone)."""
        fixed_utc = datetime(2026, 7, 18, 3, 30, tzinfo=timezone.utc)  # 11:30 PM ET Jul 17

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc.astimezone(tz) if tz else fixed_utc

        monkeypatch.setattr("kalshi_bots.agents.analyst.datetime", FrozenDatetime)
        analyst = Analyst(vault, kalshi=None, espn=FakeEspn([]))
        analyst.nightly_slate(["mlb"])
        # ET "today" at this instant is still Jul 17 -> preview should be Jul 18
        note = vault.read_note("03-market-context/daily-slate/2026-07-18-preview.md")
        assert note.frontmatter["date"] == "2026-07-18"

    def test_scoreboard_failure_reported_not_raised(self, vault):
        class BrokenEspn:
            def get_scoreboard(self, league):
                raise RuntimeError("espn down")

        analyst = Analyst(vault, kalshi=None, espn=BrokenEspn())
        analyst.nightly_slate(["mlb"], for_day=date(2026, 7, 17))
        note = vault.read_note("03-market-context/daily-slate/2026-07-17-preview.md")
        assert "scoreboard unavailable" in note.body
