from datetime import date, datetime, timezone

import pytest

from kalshi_bots.league_config import parse_league_config
from kalshi_bots.skills.league_matching import (
    LeagueMatcher, TickerGrammarError, parse_event_ticker,
)
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import GameState, MarketRef, TeamRef


@pytest.fixture(scope="module")
def cfg_mlb():
    return parse_league_config(Vault())["mlb"]


def mk_market(event_ticker, yes_abbr, league="mlb"):
    return MarketRef(league=league, series_ticker=event_ticker.split("-")[0],
                     event_ticker=event_ticker,
                     market_ticker=f"{event_ticker}-{yes_abbr}",
                     yes_team_kalshi_abbr=yes_abbr, title="", close_ts=None,
                     settlement_notes=None)


def mk_game(eid, home_abbr, away_abbr, start_utc, league="mlb"):
    return GameState(
        league=league, espn_event_id=eid, status="scheduled",
        home=TeamRef(league, home_abbr, None, home_abbr),
        away=TeamRef(league, away_abbr, None, away_abbr),
        home_score=0, away_score=0, period=0, period_half=None,
        clock_seconds=None, win_prob_home=None, win_prob_source_ts=None,
        start_time=start_utc, fetched_at=start_utc,
    )


class FakeKalshi:
    def __init__(self, markets):
        self.markets = markets

    def get_markets(self, series_ticker, status="open", league=""):
        return self.markets


def matcher(markets):
    return LeagueMatcher(Vault(), FakeKalshi(markets))


class TestTickerParser:
    def test_fixture_mlb_normal(self, cfg_mlb):
        """Captured live 2026-07-17: LAD @ NYY Jul 19 7:20 PM ET."""
        p = parse_event_ticker("KXMLBGAME-26JUL191920LADNYY", cfg_mlb)
        assert p.away_kalshi_abbr == "LAD" and p.home_kalshi_abbr == "NYY"
        # 19:20 ET = 23:20 UTC in July (EDT)
        assert p.start_time == datetime(2026, 7, 19, 23, 20, tzinfo=timezone.utc)
        assert p.yes_team_kalshi_abbr is None

    def test_fixture_wsh(self, cfg_mlb):
        """Captured live: WSH @ ATH with market suffix."""
        p = parse_event_ticker("KXMLBGAME-26JUL191605WSHATH-WSH", cfg_mlb)
        assert p.away_kalshi_abbr == "WSH" and p.home_kalshi_abbr == "ATH"
        assert p.yes_team_kalshi_abbr == "WSH"

    def test_fixture_ath_legacy_oak_raises(self, cfg_mlb):
        with pytest.raises(TickerGrammarError):
            parse_event_ticker("KXMLBGAME-26JUL191605WSHOAK", cfg_mlb)

    def test_bad_series_raises(self, cfg_mlb):
        with pytest.raises(TickerGrammarError):
            parse_event_ticker("KXNBAGAME-26JUL191605WSHATH", cfg_mlb)

    def test_doubleheader_g2_suffix(self, cfg_mlb):
        """Captured live 2026-07-17: TB@BOS doubleheader game 2."""
        p = parse_event_ticker("KXMLBGAME-26JUL171910TBBOSG2", cfg_mlb)
        assert p.away_kalshi_abbr == "TB" and p.home_kalshi_abbr == "BOS"
        assert p.game_number == 2

    def test_kalshi_divergent_abbrs(self, cfg_mlb):
        """Captured live 2026-07-17: Kalshi AZ (not ARI) and CWS (not CHW)."""
        p = parse_event_ticker("KXMLBGAME-26JUL191610STLAZ", cfg_mlb)
        assert p.away_kalshi_abbr == "STL" and p.home_kalshi_abbr == "AZ"
        p2 = parse_event_ticker("KXMLBGAME-26JUL191215CWSTOR", cfg_mlb)
        assert p2.away_kalshi_abbr == "CWS" and p2.home_kalshi_abbr == "TOR"
        assert cfg_mlb.by_espn("ARI").kalshi_abbr == "AZ"
        assert cfg_mlb.by_espn("CHW").kalshi_abbr == "CWS"

    def test_dst_winter(self, cfg_mlb):
        # 19:20 ET in November = EST = 00:20 UTC next day
        p = parse_event_ticker("KXMLBGAME-26NOV011920LADNYY", cfg_mlb)
        assert p.start_time == datetime(2026, 11, 2, 0, 20, tzinfo=timezone.utc)


class TestResolve:
    START = datetime(2026, 7, 19, 23, 20, tzinfo=timezone.utc)

    def test_fixture_mlb_normal_match(self):
        markets = [mk_market("KXMLBGAME-26JUL191920LADNYY", "NYY"),
                   mk_market("KXMLBGAME-26JUL191920LADNYY", "LAD")]
        r = matcher(markets).resolve(mk_game("401", "NYY", "LAD", self.START))
        assert r.market is not None
        assert r.method == "alias_exact"
        assert r.market.yes_team_kalshi_abbr == "NYY"  # canonical = home YES

    def test_fixture_et_utc_rollover(self):
        # 22:05 ET Jul 19 = 02:05 UTC Jul 20
        markets = [mk_market("KXMLBGAME-26JUL192205SEALAA", "LAA")]
        game = mk_game("402", "LAA", "SEA",
                       datetime(2026, 7, 20, 2, 5, tzinfo=timezone.utc))
        r = matcher(markets).resolve(game)
        assert r.market is not None and r.method == "alias_exact"

    def test_fixture_mlb_doubleheader_distinct_times(self):
        markets = [mk_market("KXMLBGAME-26JUL191305NYMPHI", "PHI"),
                   mk_market("KXMLBGAME-26JUL191910NYMPHI", "PHI")]
        game1 = mk_game("g1", "PHI", "NYM",
                        datetime(2026, 7, 19, 17, 5, tzinfo=timezone.utc))   # 13:05 ET
        game2 = mk_game("g2", "PHI", "NYM",
                        datetime(2026, 7, 19, 23, 10, tzinfo=timezone.utc))  # 19:10 ET
        m = matcher(markets)
        r1, r2 = m.resolve(game1), m.resolve(game2)
        assert r1.method == "alias_plus_start_time"
        assert r1.market.event_ticker == "KXMLBGAME-26JUL191305NYMPHI"
        assert r2.market.event_ticker == "KXMLBGAME-26JUL191910NYMPHI"

    def test_two_candidates_inside_window_ambiguous(self):
        # game at 13:10 ET; two events 13:05 and 13:15 ET -> both within 45 min,
        # closest is 13:05 (5 min) vs 13:15 (5 min): tie -> ambiguous
        markets = [mk_market("KXMLBGAME-26JUL191305NYMPHI", "PHI"),
                   mk_market("KXMLBGAME-26JUL191315NYMPHI", "PHI")]
        game = mk_game("g3", "PHI", "NYM",
                       datetime(2026, 7, 19, 17, 10, tzinfo=timezone.utc))
        r = matcher(markets).resolve(game)
        assert r.market is None and r.ambiguous is True
        assert r.candidates_considered == 2

    def test_single_candidate_outside_window_rejected(self):
        """Field regression 2026-07-17: doubleheader game 1 finished, its market
        settled/closed; only the G2 market (19:10 ET) remains open. Game 1
        (13:35 ET) must NOT match it just because it's the only candidate."""
        markets = [mk_market("KXMLBGAME-26JUL171910TBBOSG2", "BOS")]
        game1 = mk_game("401872178", "BOS", "TB",
                        datetime(2026, 7, 17, 17, 35, tzinfo=timezone.utc))
        r = matcher(markets).resolve(game1)
        assert r.market is None
        assert r.note == "start-time-outside-window"
        # game 2 at its scheduled time still matches
        game2 = mk_game("401816145", "BOS", "TB",
                        datetime(2026, 7, 17, 23, 10, tzinfo=timezone.utc))
        r2 = matcher(markets).resolve(game2)
        assert r2.market is not None

    def test_fixture_unmatched_exhibition(self):
        r = matcher([]).resolve(mk_game("g4", "BOS", "TB", self.START))
        assert r.market is None and r.ambiguous is False
        assert r.method == "none"

    def test_fixture_unverified_league_nfl(self):
        game = mk_game("g5", "KC", "BUF", self.START, league="nfl")
        r = matcher([]).resolve(game)
        assert r.market is None and r.note == "grammar-unverified"

    def test_grammar_error_recorded_not_fatal(self):
        markets = [mk_market("KXMLBGAME-26JUL191920LADNYY", "NYY"),
                   mk_market("KXMLBGAME-26JUL19XXXXBAD", "BAD")]
        m = matcher(markets)
        r = m.resolve(mk_game("401", "NYY", "LAD", self.START))
        assert r.market is not None       # good ticker still matches
        assert m.grammar_errors           # alarm recorded loudly

    def test_slate_dupe_assignment_voided(self):
        markets = [mk_market("KXMLBGAME-26JUL191920LADNYY", "NYY")]
        g1 = mk_game("a1", "NYY", "LAD", self.START)
        g2 = mk_game("a2", "NYY", "LAD", self.START)  # duplicate ESPN listing
        m = matcher(markets)
        results = m.resolve_slate("mlb", date(2026, 7, 19), [g1, g2])
        assert all(r.market is None for r in results.values())
        assert all(r.note == "dupe-assignment" for r in results.values())
