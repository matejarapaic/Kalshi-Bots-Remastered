"""espn-data skill. Spec: skills/espn-data/SKILL.md.

Primary live game-state source. Polls ESPN scoreboard/summary/injuries,
normalizes to GameState/InjuryEvent, and provides swing/decided/injury
detectors. League-generic: everything league-specific comes from
league-config.md via the vault skill.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests

from kalshi_bots.league_config import LeagueConfig, parse_league_config
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import (
    DecidedEvent, GameDetail, GameState, InjuryEvent, SwingEvent, TeamRef,
)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{slug}/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/{slug}/summary?event={event_id}"

# Spec Configuration
SWING_PTS = 0.15
SWING_WINDOW_MIN = 4
BUFFER_MINUTES = 12
STARTER_EXIT_INNING_MAX = 5

STATUS_MAP = {  # spec rule 6; unknown -> suspended (fail-safe)
    "STATUS_SCHEDULED": "scheduled",
    "STATUS_IN_PROGRESS": "in_progress",
    "STATUS_FINAL": "final",
    "STATUS_POSTPONED": "postponed",
    "STATUS_SUSPENDED": "suspended",
    "STATUS_DELAYED": "in_progress",
    "STATUS_RAIN_DELAY": "in_progress",
    "STATUS_END_PERIOD": "in_progress",
    "STATUS_HALFTIME": "in_progress",
}

INJURY_STATUS_MAP = {
    "out": "OUT", "doubtful": "DOUBTFUL", "questionable": "QUESTIONABLE",
    "probable": "PROBABLE", "day-to-day": "DAY_TO_DAY", "active": "ACTIVE",
}


class EspnDataError(Exception):
    pass


class EspnFeedStale(EspnDataError):
    pass


class EspnParseError(EspnDataError):
    pass


def is_stale(fetched_at: datetime, max_age_s: int,
             now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return (now - fetched_at).total_seconds() > max_age_s


def _parse_clock(display: str | None) -> int | None:
    if not display:
        return None
    m = re.match(r"^(\d+):(\d{2})", str(display))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class EspnData:
    def __init__(self, vault: Vault, session: requests.Session | None = None):
        self.vault = vault
        self.session = session or requests.Session()
        self._last_req: dict[str, float] = {}
        self._buffers: dict[str, deque] = {}          # event_id -> (ts, win_prob_home)
        self._buffer_lock = threading.Lock()
        self._injury_snapshots: dict[str, dict] = {}  # event_id -> {player_id: status}
        self._pitchers: dict[str, dict] = {}          # event_id -> tracking state

    def _config(self) -> dict[str, LeagueConfig]:
        return parse_league_config(self.vault)

    def _get(self, url: str) -> dict:
        # politeness: <=1 req/s per endpoint host+path prefix
        key = url.split("?")[0]
        wait = self._last_req.get(key, 0) + 1.0 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        backoff = 1.0
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=15)
            except requests.RequestException as e:
                if attempt == 2:
                    raise EspnDataError(f"fetch failed: {e}") from e
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            finally:
                self._last_req[key] = time.monotonic()
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 2:
                    raise EspnDataError(f"HTTP {resp.status_code}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            if resp.status_code != 200:
                raise EspnDataError(f"HTTP {resp.status_code} for {url}")
            return resp.json()
        raise EspnDataError("unreachable")

    # --- parsing (pure; fixtures test these directly) ---

    def parse_scoreboard_event(self, league: str, event: dict,
                               fetched_at: datetime | None = None) -> GameState:
        fetched_at = fetched_at or datetime.now(timezone.utc)
        comp = event["competitions"][0]
        status_type = comp.get("status", {}).get("type", {})
        status_name = status_type.get("name", "")
        status = STATUS_MAP.get(status_name, "suspended")

        home = away = None
        home_score = away_score = 0
        for c in comp.get("competitors", []):
            team = TeamRef(league=league, espn_abbr=c["team"].get("abbreviation", ""),
                           kalshi_abbr=None, display_name=c["team"].get("displayName", ""))
            raw_score = c.get("score", "")
            if raw_score in ("", None):
                if status != "scheduled":
                    raise EspnParseError(f"missing score in non-scheduled game {event.get('id')}")
                score = 0
            else:
                score = int(raw_score)
            if c.get("homeAway") == "home":
                home, home_score = team, score
            else:
                away, away_score = team, score
        if home is None or away is None:
            raise EspnParseError(f"competitors incomplete for event {event.get('id')}")

        period = int(comp.get("status", {}).get("period", 0) or 0)
        period_half = None
        clock_seconds = None
        if league == "mlb":
            detail = status_type.get("detail", "")
            m = re.match(r"^(Top|Bottom|Bot|Mid|End)\w*\s+(\d+)", detail)
            if m:
                period_half = "top" if m.group(1) in ("Top", "Mid") else "bottom"
                period = int(m.group(2))
        else:
            clock_seconds = _parse_clock(comp.get("status", {}).get("displayClock"))

        state = GameState(
            league=league, espn_event_id=str(event["id"]), status=status,
            home=home, away=away, home_score=home_score, away_score=away_score,
            period=period, period_half=period_half, clock_seconds=clock_seconds,
            win_prob_home=None, win_prob_source_ts=None,
            start_time=_parse_iso(event["date"]), fetched_at=fetched_at,
        )

        # MLB starter-exit tracking (spec rule 10)
        if league == "mlb" and status == "in_progress":
            situation = comp.get("situation", {})
            pitcher = (situation.get("pitcher") or {}).get("athlete") or situation.get("pitcher")
            pid = str(pitcher.get("id")) if isinstance(pitcher, dict) and pitcher.get("id") else None
            if pid and period_half:
                defense = "home" if period_half == "top" else "away"
                st = self._pitchers.setdefault(state.espn_event_id, {})
                st.setdefault(f"starter_{defense}", pid)
                st[f"current_{defense}"] = pid
                st["period"] = period
        return state

    def parse_summary(self, league: str, espn_event_id: str, summary: dict,
                      state: GameState) -> GameDetail:
        wp = summary.get("winprobability") or []
        if wp:
            last = wp[-1]
            state.win_prob_home = float(last.get("homeWinPercentage"))
        tie = float(wp[-1].get("tiePercentage", 0.0)) if wp else 0.0
        return GameDetail(state=state, win_prob_series_len=len(wp),
                          tie_risk=tie > 0.01, starting_pitcher_ids=None)

    def parse_injuries(self, league: str, espn_event_id: str, summary: dict,
                       fetched_at: datetime | None = None) -> list[InjuryEvent]:
        fetched_at = fetched_at or datetime.now(timezone.utc)
        out = []
        for team_block in summary.get("injuries", []) or []:
            team_raw = team_block.get("team", {})
            team = TeamRef(league=league, espn_abbr=team_raw.get("abbreviation", ""),
                           kalshi_abbr=None, display_name=team_raw.get("displayName", ""))
            for inj in team_block.get("injuries", []) or []:
                athlete = inj.get("athlete", {}) or {}
                status_raw = str(inj.get("status", "")).lower()
                status = INJURY_STATUS_MAP.get(status_raw)
                if status is None:
                    continue  # graceful degradation on unknown statuses
                src = inj.get("date")
                out.append(InjuryEvent(
                    league=league, team=team, espn_event_id=espn_event_id,
                    player_id=str(athlete.get("id", "")),
                    player_name=athlete.get("displayName", ""),
                    position=(athlete.get("position") or {}).get("abbreviation", ""),
                    status=status,
                    source_ts=_parse_iso(src) if src else None,
                    fetched_at=fetched_at,
                ))
        return out

    # --- fetchers ---

    def get_scoreboard(self, league: str) -> list[GameState]:
        cfg = self._config()[league]
        raw = self._get(SCOREBOARD_URL.format(slug=cfg.espn_slug))
        fetched_at = datetime.now(timezone.utc)
        return [self.parse_scoreboard_event(league, e, fetched_at)
                for e in raw.get("events", [])]

    def get_game_detail(self, league: str, espn_event_id: str,
                        state: GameState) -> GameDetail:
        cfg = self._config()[league]
        raw = self._get(SUMMARY_URL.format(slug=cfg.espn_slug, event_id=espn_event_id))
        return self.parse_summary(league, espn_event_id, raw, state)

    def get_injuries(self, league: str, espn_event_id: str) -> list[InjuryEvent]:
        cfg = self._config()[league]
        raw = self._get(SUMMARY_URL.format(slug=cfg.espn_slug, event_id=espn_event_id))
        return self.parse_injuries(league, espn_event_id, raw)

    # --- detectors ---

    def record_poll(self, game: GameState) -> None:
        if game.win_prob_home is None:
            return
        with self._buffer_lock:
            buf = self._buffers.setdefault(game.espn_event_id, deque())
            if buf and (game.fetched_at - buf[-1][0]).total_seconds() > 6 * 3600:
                buf.clear()  # suspended/resumed: reset (spec edge case)
            buf.append((game.fetched_at, game.win_prob_home))
            cutoff = game.fetched_at.timestamp() - BUFFER_MINUTES * 60
            while buf and buf[0][0].timestamp() < cutoff:
                buf.popleft()

    def detect_swing(self, espn_event_id: str) -> SwingEvent | None:
        with self._buffer_lock:
            buf = list(self._buffers.get(espn_event_id, ()))
        if len(buf) < 2:
            return None
        now_ts, now_prob = buf[-1]
        window_cutoff = now_ts.timestamp() - SWING_WINDOW_MIN * 60
        best = None
        for ts, prob in buf[:-1]:
            if ts.timestamp() < window_cutoff:
                continue
            delta = now_prob - prob
            if abs(delta) >= SWING_PTS and (best is None or abs(delta) > abs(best[1])):
                best = ((ts, prob), delta)
        if best is None:
            return None
        (from_ts, from_prob), delta = best
        return SwingEvent(
            espn_event_id=espn_event_id,
            direction="home" if delta > 0 else "away",
            magnitude=abs(delta),
            window_s=int((now_ts - from_ts).total_seconds()),
            from_prob=from_prob, to_prob=now_prob, tie_prob=0.0,
            detected_at=now_ts,
        )

    def detect_decided(self, game: GameState, outs: int | None = None) -> DecidedEvent | None:
        """Confirmed garbage-time league rules. `outs` (MLB, from situation)
        is optional; without it the 2-outs-in-9th branch cannot fire."""
        if game.status != "in_progress" or game.win_prob_home is None:
            return None
        wp_home = game.win_prob_home
        leader = "home" if game.home_score > game.away_score else "away"
        lead = abs(game.home_score - game.away_score)
        wp_leader = wp_home if leader == "home" else 1 - wp_home
        if wp_leader < 0.98 or lead == 0:
            return None
        rule = None
        if game.league == "nfl":
            if game.period >= 4 and (game.clock_seconds or 9999) <= 360 and lead >= 17:
                rule = "nfl_lead17_under6min"
        elif game.league == "nba":
            clock = game.clock_seconds or 9999
            if game.period >= 4 and lead >= 15 and clock <= 240:
                rule = "nba_lead15_under4min"
            elif game.period >= 4 and lead >= 9 and clock <= 60:
                rule = "nba_lead9_under1min"
        elif game.league == "mlb":
            if game.period >= 9 and lead >= 5:
                rule = "mlb_lead5_9th"
            elif game.period >= 9 and lead >= 3 and outs is not None and outs >= 2:
                rule = "mlb_lead3_2out_9th"
        if rule is None:
            return None
        return DecidedEvent(espn_event_id=game.espn_event_id, leader=leader,
                            win_prob=wp_leader, rule=rule,
                            detected_at=game.fetched_at)

    def detect_injury_changes(self, league: str, espn_event_id: str,
                              current: list[InjuryEvent]) -> list[InjuryEvent]:
        prev = self._injury_snapshots.get(espn_event_id, {})
        changed = [e for e in current
                   if prev.get(e.player_id) != e.status]
        self._injury_snapshots[espn_event_id] = {e.player_id: e.status for e in current}

        # MLB starter exit synthesis (spec rule 10)
        st = self._pitchers.get(espn_event_id, {})
        for side in ("home", "away"):
            starter, now_p = st.get(f"starter_{side}"), st.get(f"current_{side}")
            if (starter and now_p and starter != now_p
                    and st.get("period", 99) <= STARTER_EXIT_INNING_MAX
                    and not st.get(f"exit_emitted_{side}")):
                st[f"exit_emitted_{side}"] = True
                changed.append(InjuryEvent(
                    league=league,
                    team=TeamRef(league=league, espn_abbr="", kalshi_abbr=None,
                                 display_name=f"{side} team"),
                    espn_event_id=espn_event_id, player_id=starter,
                    player_name="(starting pitcher)", position="SP", status="OUT",
                    source_ts=None, fetched_at=datetime.now(timezone.utc),
                ))
        return changed
