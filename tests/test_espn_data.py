import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kalshi_bots.skills.espn_data import (
    EspnData, STATUS_MAP, _parse_clock, is_stale,
)
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import GameState, TeamRef

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def real_vault():
    return Vault()  # the actual kalshi-vault (read-only usage here)


@pytest.fixture
def espn(real_vault):
    return EspnData(real_vault)


def make_game(league="mlb", status="in_progress", home_score=9, away_score=0,
              period=9, half="bottom", clock=None, wp_home=0.99, event_id="e1"):
    return GameState(
        league=league, espn_event_id=event_id, status=status,
        home=TeamRef(league, "BOS", None, "Boston Red Sox"),
        away=TeamRef(league, "TB", None, "Tampa Bay Rays"),
        home_score=home_score, away_score=away_score, period=period,
        period_half=half if league == "mlb" else None,
        clock_seconds=clock, win_prob_home=wp_home, win_prob_source_ts=None,
        start_time=NOW - timedelta(hours=2), fetched_at=NOW,
    )


class TestLiveCaptureParsing:
    """Against the real payload captured live 2026-07-17 (TB @ BOS, Bot 7th)."""

    def test_scoreboard_event(self, espn):
        raw = json.loads((FIXTURES / "espn_mlb_scoreboard.json").read_text())
        state = espn.parse_scoreboard_event("mlb", raw["events"][0], NOW)
        assert state.espn_event_id == "401872178"
        assert state.status == "in_progress"
        assert state.home.espn_abbr == "BOS" and state.away.espn_abbr == "TB"
        assert state.home_score == 9 and state.away_score == 0
        assert state.period == 7 and state.period_half == "bottom"
        assert state.clock_seconds is None  # MLB has no clock

    def test_summary_win_prob(self, espn):
        raw = json.loads((FIXTURES / "espn_mlb_summary.json").read_text())
        state = make_game(wp_home=None)
        detail = espn.parse_summary("mlb", "401872178", raw, state)
        assert detail.win_prob_series_len == 56
        assert state.win_prob_home == pytest.approx(0.999)
        assert detail.tie_risk is False

    def test_injuries_parse(self, espn):
        raw = json.loads((FIXTURES / "espn_mlb_summary.json").read_text())
        events = espn.parse_injuries("mlb", "401872178", raw, NOW)
        assert isinstance(events, list)  # payload may map to 0+ after status filtering
        for e in events:
            assert e.status in ("OUT", "DOUBTFUL", "QUESTIONABLE", "PROBABLE",
                                "DAY_TO_DAY", "ACTIVE")


class TestStatusNormalization:
    @pytest.mark.parametrize("espn_status,expected", [
        ("STATUS_SCHEDULED", "scheduled"),
        ("STATUS_IN_PROGRESS", "in_progress"),
        ("STATUS_FINAL", "final"),
        ("STATUS_POSTPONED", "postponed"),
        ("STATUS_RAIN_DELAY", "in_progress"),
    ])
    def test_known(self, espn_status, expected):
        assert STATUS_MAP[espn_status] == expected

    def test_unknown_maps_suspended(self, espn):
        event = {"id": "x", "date": "2026-07-17T17:35Z", "competitions": [{
            "status": {"type": {"name": "STATUS_SOMETHING_NEW"}, "period": 1},
            "competitors": [
                {"homeAway": "home", "team": {"abbreviation": "A", "displayName": "A"}, "score": "0"},
                {"homeAway": "away", "team": {"abbreviation": "B", "displayName": "B"}, "score": "0"},
            ]}]}
        assert espn.parse_scoreboard_event("mlb", event, NOW).status == "suspended"


class TestClock:
    def test_parse(self):
        assert _parse_clock("5:24") == 324
        assert _parse_clock("0:59") == 59
        assert _parse_clock(None) is None
        assert _parse_clock("halftime") is None


class TestSwingDetector:
    def _feed(self, espn, series):
        for minutes_ago, prob in series:
            g = make_game(wp_home=prob)
            g.fetched_at = NOW - timedelta(minutes=minutes_ago)
            espn.record_poll(g)

    def test_jump_within_window_fires(self, espn):
        self._feed(espn, [(3, 0.50), (1, 0.58), (0, 0.66)])
        ev = espn.detect_swing("e1")
        assert ev is not None and ev.direction == "home"
        assert ev.magnitude == pytest.approx(0.16)

    def test_gradual_drift_no_fire(self, espn):
        self._feed(espn, [(10, 0.50), (8, 0.55), (6, 0.60), (3, 0.63), (0, 0.66)])
        assert espn.detect_swing("e1") is None  # 16pts but over 10 min

    def test_away_direction(self, espn):
        self._feed(espn, [(2, 0.70), (0, 0.50)])
        ev = espn.detect_swing("e1")
        assert ev.direction == "away" and ev.magnitude == pytest.approx(0.20)

    def test_none_win_prob_skipped(self, espn):
        g = make_game(wp_home=None)
        espn.record_poll(g)
        assert espn.detect_swing("e1") is None


class TestDecidedDetector:
    def test_mlb_lead5_9th(self, espn):
        ev = espn.detect_decided(make_game(home_score=9, away_score=0, period=9))
        assert ev is not None and ev.rule == "mlb_lead5_9th" and ev.leader == "home"

    def test_mlb_lead3_needs_outs(self, espn):
        g = make_game(home_score=4, away_score=1, period=9)
        assert espn.detect_decided(g) is None
        assert espn.detect_decided(g, outs=2).rule == "mlb_lead3_2out_9th"

    def test_mlb_8th_inning_no_fire(self, espn):
        assert espn.detect_decided(make_game(period=8)) is None

    def test_nfl_rule(self, espn):
        g = make_game(league="nfl", home_score=31, away_score=10, period=4,
                      half=None, clock=300, wp_home=0.99)
        assert espn.detect_decided(g).rule == "nfl_lead17_under6min"

    def test_nfl_lead16_no_fire(self, espn):
        g = make_game(league="nfl", home_score=30, away_score=14, period=4,
                      half=None, clock=300, wp_home=0.99)
        assert espn.detect_decided(g) is None  # lead 16 < 17

    def test_nba_rules(self, espn):
        g = make_game(league="nba", home_score=110, away_score=94, period=4,
                      half=None, clock=200, wp_home=0.99)
        assert espn.detect_decided(g).rule == "nba_lead15_under4min"
        g2 = make_game(league="nba", home_score=104, away_score=95, period=4,
                       half=None, clock=50, wp_home=0.99)
        assert espn.detect_decided(g2).rule == "nba_lead9_under1min"

    def test_win_prob_gate(self, espn):
        assert espn.detect_decided(make_game(wp_home=0.97)) is None

    def test_none_win_prob_never_decides(self, espn):
        assert espn.detect_decided(make_game(wp_home=None)) is None


class TestInjuryDiffing:
    def _inj(self, pid, status):
        from kalshi_bots.types import InjuryEvent
        return InjuryEvent(league="mlb",
                           team=TeamRef("mlb", "BOS", None, "Boston Red Sox"),
                           espn_event_id="e1", player_id=pid, player_name="P",
                           position="SP", status=status, source_ts=None,
                           fetched_at=NOW)

    def test_new_out_flagged_once(self, espn):
        first = espn.detect_injury_changes("mlb", "e1", [self._inj("p1", "OUT")])
        assert len(first) == 1
        second = espn.detect_injury_changes("mlb", "e1", [self._inj("p1", "OUT")])
        assert second == []

    def test_status_transition(self, espn):
        espn.detect_injury_changes("mlb", "e1", [self._inj("p1", "QUESTIONABLE")])
        changed = espn.detect_injury_changes("mlb", "e1", [self._inj("p1", "OUT")])
        assert len(changed) == 1 and changed[0].status == "OUT"

    def test_mlb_starter_exit(self, espn):
        espn._pitchers["e1"] = {"starter_home": "sp1", "current_home": "sp2", "period": 4}
        changed = espn.detect_injury_changes("mlb", "e1", [])
        assert len(changed) == 1 and changed[0].position == "SP"
        # emitted once only
        assert espn.detect_injury_changes("mlb", "e1", []) == []

    def test_starter_exit_6th_inning_no_fire(self, espn):
        espn._pitchers["e1"] = {"starter_home": "sp1", "current_home": "sp2", "period": 6}
        assert espn.detect_injury_changes("mlb", "e1", []) == []


class TestStaleness:
    def test_is_stale(self):
        assert is_stale(NOW - timedelta(seconds=91), 90, now=NOW)
        assert not is_stale(NOW - timedelta(seconds=89), 90, now=NOW)


class TestLeagueConfigParsing:
    def test_all_leagues(self, real_vault):
        from kalshi_bots.league_config import parse_league_config
        cfgs = parse_league_config(real_vault)
        assert set(cfgs) == {"nfl", "nba", "mlb"}
        assert cfgs["mlb"].espn_slug == "baseball/mlb"
        assert cfgs["mlb"].series_ticker == "KXMLBGAME"
        assert cfgs["mlb"].grammar_verified is True
        assert cfgs["nfl"].grammar_verified is False
        assert len(cfgs["nfl"].aliases) == 32
        assert len(cfgs["nba"].aliases) == 30
        assert len(cfgs["mlb"].aliases) == 30

    def test_alias_lookups(self, real_vault):
        from kalshi_bots.league_config import parse_league_config
        cfgs = parse_league_config(real_vault)
        wsh = cfgs["mlb"].by_espn("WSH")
        assert wsh.kalshi_abbr == "WSH" and wsh.verified
        gs = cfgs["nba"].by_espn("GS")
        assert gs.kalshi_abbr == "GSW"
        assert cfgs["mlb"].by_name("Kansas City Royals").espn_abbr == "KC"
        # league scoping: NFL Cardinals vs MLB Cardinals
        assert cfgs["nfl"].by_name("Arizona Cardinals").espn_abbr == "ARI"
        assert cfgs["mlb"].by_name("Arizona Cardinals") is None
