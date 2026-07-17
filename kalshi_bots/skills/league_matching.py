"""league-matching skill. Spec: skills/league-matching/SKILL.md.

Kalshi market ticker <-> ESPN game ID resolution via the league-config alias
map. Ambiguity returns None, never a guess. Category A convention (noted in
spec review): the canonical MarketRef returned is the market whose YES side is
the HOME team; trading the away team = side "no" on that market.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from kalshi_bots.league_config import LeagueConfig, parse_league_config
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import GameState, MarketRef, MatchResult, ParsedTicker

log = logging.getLogger(__name__)

TIE_BREAK_WINDOW_MIN = 45
ET = ZoneInfo("America/New_York")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_BLOCK_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})([A-Z]+?)(G\d)?$")


class MatchingError(Exception):
    pass


class TickerGrammarError(Exception):
    pass


def parse_event_ticker(event_ticker: str, config: LeagueConfig) -> ParsedTicker:
    """KXMLBGAME-26JUL191920LADNYY -> series, away LAD, home NYY, start ET->UTC.

    Team split is longest-suffix-first against the league's known Kalshi
    abbreviations (HOME is the suffix). Unknown abbreviations raise
    TickerGrammarError — the loud alarm that the alias table is wrong.
    """
    parts = event_ticker.split("-")
    if len(parts) < 2 or parts[0] != config.series_ticker:
        raise TickerGrammarError(f"{event_ticker}: series != {config.series_ticker}")
    m = _BLOCK_RE.match(parts[1])
    if not m:
        raise TickerGrammarError(f"{event_ticker}: date-time block unparseable")
    yy, mon, dd, hh, mm, teams, gsuf = m.groups()
    if mon not in _MONTHS:
        raise TickerGrammarError(f"{event_ticker}: bad month {mon}")
    known = sorted({r.kalshi_abbr for r in config.aliases}, key=len, reverse=True)
    home = next((k for k in known if teams.endswith(k)), None)
    if home:
        away = teams[: -len(home)]
        if away not in known:
            home = away = None
    if not home:
        raise TickerGrammarError(
            f"{event_ticker}: team block {teams!r} does not split into two known "
            f"Kalshi abbreviations — alias table may be wrong")
    start_et = datetime(2000 + int(yy), _MONTHS[mon], int(dd), int(hh), int(mm), tzinfo=ET)
    return ParsedTicker(series_ticker=parts[0], away_kalshi_abbr=away,
                        home_kalshi_abbr=home,
                        start_time=start_et.astimezone(timezone.utc),
                        yes_team_kalshi_abbr=parts[2] if len(parts) > 2 else None,
                        game_number=int(gsuf[1:]) if gsuf else None)


class LeagueMatcher:
    def __init__(self, vault: Vault, kalshi_client):
        self.vault = vault
        self.kalshi = kalshi_client
        self._slate_cache: dict[tuple[str, str], dict[str, MatchResult]] = {}
        self._market_cache: dict[tuple[str, str], list[MarketRef]] = {}
        self.grammar_errors: list[str] = []

    def _config(self, league: str) -> LeagueConfig:
        cfgs = parse_league_config(self.vault)
        if league not in cfgs:
            raise MatchingError(f"league {league!r} not in league-config")
        return cfgs[league]

    def _markets(self, league: str, day: date) -> list[MarketRef]:
        key = (league, day.isoformat())
        if key not in self._market_cache:
            cfg = self._config(league)
            self._market_cache[key] = self.kalshi.get_markets(
                cfg.series_ticker, status="open", league=league)
        return self._market_cache[key]

    def resolve(self, game: GameState) -> MatchResult:
        day = game.start_time.astimezone(ET).date()
        slate = self._slate_cache.get((game.league, day.isoformat()))
        if slate and game.espn_event_id in slate:
            return slate[game.espn_event_id]
        return self._resolve_one(game)

    def _resolve_one(self, game: GameState) -> MatchResult:
        cfg = self._config(game.league)
        eid = game.espn_event_id

        if not cfg.grammar_verified:
            return MatchResult(espn_event_id=eid, market=None, method="none",
                               ambiguous=False, candidates_considered=0,
                               note="grammar-unverified")

        home_row = cfg.by_espn(game.home.espn_abbr)
        away_row = cfg.by_espn(game.away.espn_abbr)
        if home_row is None or away_row is None:
            raise MatchingError(
                f"ESPN abbr unresolvable in league-config: "
                f"{game.home.espn_abbr}/{game.away.espn_abbr} ({game.league})")

        day = game.start_time.astimezone(ET).date()
        # group candidate markets by event; keep team-pair matches
        events: dict[str, dict] = {}
        for mkt in self._markets(game.league, day):
            if mkt.event_ticker in events:
                events[mkt.event_ticker]["markets"].append(mkt)
                continue
            try:
                parsed = parse_event_ticker(mkt.event_ticker, cfg)
            except TickerGrammarError as e:
                if str(e) not in self.grammar_errors:
                    self.grammar_errors.append(str(e))
                    log.error("ticker grammar error (alias table wrong?): %s", e)
                continue
            events[mkt.event_ticker] = {"parsed": parsed, "markets": [mkt]}

        candidates = []
        for ev in events.values():
            p = ev["parsed"]
            pair_match = (p.home_kalshi_abbr == home_row.kalshi_abbr
                          and p.away_kalshi_abbr == away_row.kalshi_abbr)
            reversed_match = (p.home_kalshi_abbr == away_row.kalshi_abbr
                              and p.away_kalshi_abbr == home_row.kalshi_abbr)
            # date proximity: embedded start within ±1 day of game start
            if abs((p.start_time - game.start_time).total_seconds()) > 86400:
                continue
            if pair_match or reversed_match:
                if reversed_match:
                    log.warning("home/away order mismatch for %s vs ESPN %s "
                                "(grammar sanity signal)", p, eid)
                candidates.append(ev)

        considered = len(candidates)
        if considered == 0:
            return MatchResult(espn_event_id=eid, market=None, method="none",
                               ambiguous=False, candidates_considered=0)

        def canonical(ev) -> MarketRef | None:
            for mkt in ev["markets"]:
                if mkt.yes_team_kalshi_abbr == home_row.kalshi_abbr:
                    return mkt
            return ev["markets"][0] if ev["markets"] else None

        window_s = TIE_BREAK_WINDOW_MIN * 60
        if considered == 1:
            # A single candidate must still sit in the start-time window: both
            # sides are SCHEDULED times, so they agree within minutes for the
            # right game. Field-verified 2026-07-17: after a doubleheader's
            # game 1 settles, only the G2 market stays open — without this
            # check, game 1's ESPN event "alias_exact"-matches game 2's market.
            delta = abs((candidates[0]["parsed"].start_time
                         - game.start_time).total_seconds())
            if delta > window_s:
                return MatchResult(espn_event_id=eid, market=None, method="none",
                                   ambiguous=False, candidates_considered=1,
                                   note="start-time-outside-window")
            return MatchResult(espn_event_id=eid, market=canonical(candidates[0]),
                               method="alias_exact", ambiguous=False,
                               candidates_considered=1)

        # doubleheader tie-break: start-time proximity, unique closest in window
        scored = sorted(
            ((abs((ev["parsed"].start_time - game.start_time).total_seconds()), ev)
             for ev in candidates), key=lambda t: t[0])
        in_window = [t for t in scored if t[0] <= window_s]
        if len(in_window) == 1 or (len(in_window) > 1 and in_window[0][0] < in_window[1][0]):
            return MatchResult(espn_event_id=eid, market=canonical(in_window[0][1]),
                               method="alias_plus_start_time", ambiguous=False,
                               candidates_considered=considered)
        return MatchResult(espn_event_id=eid, market=None, method="none",
                           ambiguous=True, candidates_considered=considered)

    def resolve_slate(self, league: str, day: date,
                      games: list[GameState]) -> dict[str, MatchResult]:
        results = {g.espn_event_id: self._resolve_one(g) for g in games}
        # internal consistency: no market assigned to two ESPN events
        seen: dict[str, str] = {}
        for eid, r in results.items():
            if r.market is None:
                continue
            other = seen.get(r.market.market_ticker)
            if other:
                log.error("market %s matched two events (%s, %s) — voiding both",
                          r.market.market_ticker, other, eid)
                results[eid] = MatchResult(eid, None, "none", True,
                                           r.candidates_considered, note="dupe-assignment")
                results[other] = MatchResult(other, None, "none", True,
                                             results[other].candidates_considered,
                                             note="dupe-assignment")
            else:
                seen[r.market.market_ticker] = eid
        self._slate_cache[(league, day.isoformat())] = results
        return results

    def invalidate(self, league: str, day: date) -> None:
        self._slate_cache.pop((league, day.isoformat()), None)
        self._market_cache.pop((league, day.isoformat()), None)
