"""odds-api skill. Spec: skills/odds-api/SKILL.md.

Sportsbook consensus via The Odds API v4. American format is requested
explicitly and is the ONLY format the parser accepts — format autodetection is
forbidden (a decimal 1.91 misread as +191 silently flips probabilities).
De-vig per book first, then aggregate.
"""
from __future__ import annotations

import itertools
import os
import time
from datetime import datetime, timezone

import requests

from kalshi_bots.league_config import parse_league_config
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import BookQuote, ConsensusOdds, Prob, TeamRef

HOST = "https://api.the-odds-api.com"
SPORT_KEYS = {"nfl": "americanfootball_nfl", "nba": "basketball_nba",
              "mlb": "baseball_mlb"}

ODDS_REGIONS = "us"
QUOTA_RESERVE = 500
STALE_BOOK_S = 90
COMMENCE_MATCH_WINDOW_H = 6
EXCLUDED_BOOKS: set[str] = set()
MIN_CONSENSUS_BOOKS = 3  # divergence skill's gate; consumers gate on book_count


class OddsApiError(Exception):
    pass


class OddsTeamResolutionError(OddsApiError):
    pass


class OddsQuotaExceeded(OddsApiError):
    pass


class OddsFormatError(OddsApiError):
    pass


def american_to_prob(odds: int) -> Prob:
    """+150 -> 0.400; -150 -> 0.600. Raises on invalid (-100, 100) interval."""
    if not isinstance(odds, int) or -100 < odds < 100:
        raise ValueError(f"invalid American odds: {odds!r}")
    if odds > 0:
        return 100 / (odds + 100)
    return -odds / (-odds + 100)


def decimal_to_prob(d: float) -> Prob:
    """Fixtures/tests only; the live path never parses decimal (spec rule 2)."""
    if d <= 1.0:
        raise ValueError(f"invalid decimal odds: {d!r}")
    return 1 / d


def devig_two_way(p_a: Prob, p_b: Prob) -> tuple[Prob, Prob]:
    """Multiplicative normalization: p_a/(p_a+p_b). Power/Shin out of scope."""
    total = p_a + p_b
    if total <= 0:
        raise ValueError("probabilities must be positive")
    return p_a / total, p_b / total


def _validate_american(price) -> int:
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        raise OddsFormatError(f"non-numeric price {price!r}")
    if isinstance(price, float) and not price.is_integer():
        raise OddsFormatError(
            f"price {price!r} is not integer American odds — decimal payload?")
    p = int(price)
    if -100 < p < 100:
        raise OddsFormatError(f"price {p} inside (-100,100) — not American odds")
    return p


class OddsApi:
    def __init__(self, vault: Vault, session: requests.Session | None = None):
        self.vault = vault
        self.session = session or requests.Session()
        self.api_key = os.environ.get("ODDS_API_KEY", "")
        self._quota_remaining: int | None = None

    def quota_remaining(self) -> int | None:
        return self._quota_remaining

    def _get(self, path: str, params: dict) -> list:
        if not self.api_key:
            raise OddsApiError("ODDS_API_KEY not set")
        if self._quota_remaining is not None and self._quota_remaining <= 0:
            raise OddsQuotaExceeded("The Odds API quota exhausted")
        params = {**params, "apiKey": self.api_key}
        backoff = 2.0
        for attempt in range(3):
            resp = self.session.get(HOST + path, params=params, timeout=15)
            if resp.status_code == 429:
                if attempt == 2:
                    raise OddsApiError("429 after retries")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code == 401:
                raise OddsApiError("invalid ODDS_API_KEY")
            if resp.status_code != 200:
                raise OddsApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            rem = resp.headers.get("x-requests-remaining")
            if rem is not None:
                self._quota_remaining = int(float(rem))
            return resp.json()
        raise OddsApiError("unreachable")

    def get_consensus(self, league: str, home: TeamRef, away: TeamRef,
                      start_time: datetime | None = None,
                      raw_events: list | None = None) -> ConsensusOdds:
        """raw_events injectable for tests; live path fetches."""
        if raw_events is None:
            raw_events = self._get(
                f"/v4/sports/{SPORT_KEYS[league]}/odds",
                {"regions": ODDS_REGIONS, "markets": "h2h", "oddsFormat": "american"})
        fetched_at = datetime.now(timezone.utc)
        cfg = parse_league_config(self.vault)[league]

        def resolve(name: str) -> str:
            row = cfg.by_name(name)
            if row is None:
                raise OddsTeamResolutionError(
                    f"cannot resolve {name!r} in {league} alias map (no fuzzy matching)")
            return row.espn_abbr

        # find our game among events
        matches = []
        for ev in raw_events:
            try:
                ev_home, ev_away = resolve(ev["home_team"]), resolve(ev["away_team"])
            except OddsTeamResolutionError:
                continue  # other games may involve unconfigured teams
            if {ev_home, ev_away} != {home.espn_abbr, away.espn_abbr}:
                continue
            if start_time is not None:
                commence = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
                delta = abs((commence - start_time).total_seconds())
                if delta > COMMENCE_MATCH_WINDOW_H * 3600:
                    continue
                matches.append((delta, ev))
            else:
                matches.append((0.0, ev))
        if not matches:
            return ConsensusOdds(league=league, home=home, away=away,
                                 espn_event_id=None, book_count=0,
                                 devigged_home_prob=None,
                                 max_pairwise_disagreement=None, books=[],
                                 fetched_at=fetched_at)
        matches.sort(key=lambda t: t[0])
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            raise OddsApiError("two events equally close in commence_time — no guess")
        event = matches[0][1]
        is_live = start_time is not None and start_time <= fetched_at

        books: list[BookQuote] = []
        for bm in event.get("bookmakers", []):
            if bm.get("key") in EXCLUDED_BOOKS:
                continue
            source_ts = None
            if bm.get("last_update"):
                source_ts = datetime.fromisoformat(bm["last_update"].replace("Z", "+00:00"))
            if is_live and source_ts is not None and \
                    (fetched_at - source_ts).total_seconds() > STALE_BOOK_S:
                continue  # frozen in-play quote: museum piece
            h2h = [m for m in bm.get("markets", []) if m.get("key") == "h2h"]
            if not h2h:
                continue
            outcomes = h2h[0].get("outcomes", [])
            if len(outcomes) != 2:
                continue  # 3-way / tie-inclusive excluded
            try:
                probs = {}
                for o in outcomes:
                    abbr = resolve(o["name"])
                    probs[abbr] = american_to_prob(_validate_american(o["price"]))
            except OddsFormatError:
                continue  # this book excluded; whole-response error handled below
            except OddsTeamResolutionError:
                continue
            if set(probs) != {home.espn_abbr, away.espn_abbr}:
                continue
            p_home, _ = devig_two_way(probs[home.espn_abbr], probs[away.espn_abbr])
            books.append(BookQuote(book_name=bm.get("key", "?"), home_prob=p_home,
                                   fetched_at=fetched_at, source_ts=source_ts))

        if event.get("bookmakers") and not books:
            # every book failed format validation -> whole-response format error
            all_bad_format = all(
                any(not isinstance(o.get("price"), int) or -100 < o["price"] < 100
                    for m in bm.get("markets", []) if m.get("key") == "h2h"
                    for o in m.get("outcomes", []))
                for bm in event["bookmakers"] if bm.get("markets"))
            if all_bad_format:
                raise OddsFormatError("all books failed American-format validation "
                                      "— payload may be decimal; refusing to parse")

        if not books:
            return ConsensusOdds(league=league, home=home, away=away,
                                 espn_event_id=None, book_count=0,
                                 devigged_home_prob=None,
                                 max_pairwise_disagreement=None, books=[],
                                 fetched_at=fetched_at)
        probs = [b.home_prob for b in books]
        disagreement = (max(abs(a - b) for a, b in itertools.combinations(probs, 2))
                        if len(probs) > 1 else 0.0)
        return ConsensusOdds(
            league=league, home=home, away=away, espn_event_id=None,
            book_count=len(books),
            devigged_home_prob=sum(probs) / len(probs),
            max_pairwise_disagreement=disagreement, books=books,
            fetched_at=fetched_at)
