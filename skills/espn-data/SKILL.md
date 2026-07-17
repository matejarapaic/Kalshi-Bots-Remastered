# espn-data

**Trigger:** the game-monitor agent (or postmortem, in batch) needs live game state, win probability, or injury data for any configured league.

## What this is for

The primary live game-state source for the whole system (chosen over ScoreTape for reliability). It polls ESPN's public scoreboard/summary endpoints, normalizes them into `GameState`/`InjuryEvent`, and provides the derived-event *detectors* (win-prob swing, decided game, injury flag) whose outputs the game-monitor agent turns into `CandidateSignal`s. It is generic across leagues: adding a league means editing `league-config.md`, not this code.

**Honesty note:** these are undocumented public endpoints. Field paths below were verified live on 2026-07-17 (MLB, event 401872178) and must be treated as fixtures to re-verify per league in Phase 3, with graceful degradation when absent.

## Interface

```python
get_scoreboard(league: LeagueId) -> list[GameState]              # raises EspnDataError
get_game_detail(league: LeagueId, espn_event_id: str) -> GameDetail  # summary endpoint
get_injuries(league: LeagueId, espn_event_id: str) -> list[InjuryEvent]
record_poll(game: GameState) -> None            # feeds the per-game history buffer
detect_swing(espn_event_id: str) -> SwingEvent | None
detect_decided(game: GameState) -> DecidedEvent | None
detect_injury_changes(league: LeagueId, espn_event_id: str) -> list[InjuryEvent]  # new/changed only
is_stale(fetched_at: datetime, max_age_s: int) -> bool
```

Exceptions: `EspnDataError` (base), `EspnFeedStale`, `EspnParseError` (a field path moved).

## Behavior

### Endpoints (from league-config, read via the vault skill)
1. Scoreboard: `https://site.api.espn.com/apis/site/v2/sports/{slug}/scoreboard` where `{slug}` comes from league-config (`baseball/mlb`, `football/nfl`, `basketball/nba`). Summary: `.../summary?event={id}`.
2. League-config is read through the vault skill (`read_note("00-meta/league-config.md")`) at startup and on its TTL; never from disk.

### Field paths (verified live 2026-07-17, MLB)
3. Scoreboard → `events[]`: `id`, `date` (ISO, Z), `shortName` ("TB @ BOS"); `competitions[0]`: `status.type.{id,name,state,completed,detail}`, `competitors[]` with `homeAway`, `team.abbreviation`, `team.displayName`, `score` (string → int), `wasSuspended` (bool); MLB `situation`: `{lastPlay, balls, strikes, outs, pitcher, batter, onFirst, onSecond, onThird}`.
4. Summary → `winprobability[]`: items `{homeWinPercentage: float, tiePercentage: float, playId: str}` — **no wall-clock timestamps**. Consequence: swing detection cannot use ESPN's series timing; it uses our own poll-history buffer (rule 8). `GameState.win_prob_home` = last entry's `homeWinPercentage`; `win_prob_source_ts = None` (ESPN doesn't provide it; freshness rides on `fetched_at`).
5. Summary → `injuries[]`: per-team objects (`team.abbreviation` + injury list). NFL/NBA also surface pregame status via scoreboard `competitors[].team` rosters — per-league extraction table documented in code with fixtures.
6. Status normalization: `STATUS_SCHEDULED → scheduled`, `STATUS_IN_PROGRESS → in_progress`, `STATUS_FINAL → final`, `STATUS_POSTPONED → postponed`, `STATUS_SUSPENDED → suspended`, `STATUS_DELAYED → in_progress` (delay ≠ done), `STATUS_RAIN_DELAY → in_progress`. **Unknown status names map to `suspended`** (fail-safe: nothing trades on a game we can't classify) and log `EspnParseError`-level warnings.
7. Period mapping: NFL/NBA `period` = quarter, `clock_seconds` parsed from `status.displayClock`; MLB `period` = inning, `period_half` from `status.type.detail` ("Bottom 7th" → `bottom`, 7), `clock_seconds = None`.

### Detectors (primitives; game-monitor emits the CandidateSignals)
8. **Swing** (`detect_swing`): over the per-game poll buffer (ring buffer, `BUFFER_MINUTES=12`), fire when `|win_prob_home(t_now) − win_prob_home(t)| ≥ SWING_PTS` for any buffered `t` within `SWING_WINDOW_MIN` of now. Defaults `SWING_PTS=0.15`, `SWING_WINDOW_MIN=4` — these mirror the confirmed overreaction skill's trigger and are named, not inlined. Returns `SwingEvent(direction, magnitude, window_s, from_prob, to_prob)`.
9. **Decided** (`detect_decided`): implements the confirmed garbage-time league rules exactly (win prob ≥ 0.98 AND league condition: NFL lead >16 & <6:00 & possession-adjusted; NBA lead ≥15 & <4:00 or ≥9 & <1:00; MLB lead ≥5 entering 9th or ≥3 with 2 outs in 9th). Win-prob condition uses `win_prob_home` or its complement; `None` win prob → detector returns None (never decide blind).
10. **Injury** (`detect_injury_changes`): diff current `injuries[]` against the last snapshot per game; emit only transitions (new player, or status change, e.g. QUESTIONABLE→OUT). MLB in-game starter exit: `situation.pitcher.id` differs from the recorded starting pitcher before inning 5 → synthesize `InjuryEvent(status=OUT, position="SP")`. NFL/NBA in-game exits come from injury-list transitions only (play-text scraping is out of scope — documented limitation).

### Freshness & failure
11. Every output carries `fetched_at`. On fetch failure the previous value is **never re-served as fresh**: `get_*` raises, and the caller (game-monitor) writes `feed-stale` into the active-game note per its prompt. `is_stale(fetched_at, max_age_s)` is the single staleness predicate all skills share (overreaction uses 90s, divergence 60s — their notes' numbers).
12. Politeness: ≤1 req/s per endpoint with jitter; exponential backoff on 429/5xx (base 1s, ×2, max 120s). Cadence *policy* (when to poll what) belongs to the orchestrator via league-config ramp rules; this skill only exposes the fetchers.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `SWING_PTS` | 0.15 | mirrors confirmed skill note |
| `SWING_WINDOW_MIN` | 4 | mirrors confirmed skill note |
| `BUFFER_MINUTES` | 12 | poll-history ring buffer |
| `MAX_RPS_PER_ENDPOINT` | 1 | politeness |
| `STARTER_EXIT_INNING_MAX` | 5 | MLB pitcher-exit rule, mirrors injury skill |

## Edge cases
- **Doubleheaders:** two events, same teams, same date — distinct `espn_event_id`s and `start_time`s; all buffers/detectors key on event id, never on team pair.
- **Suspended/resumed games:** `wasSuspended=true` on resume; status transitions final→in_progress are possible across days — buffers reset on a >6h gap between polls of the same event.
- **Postponed after slate build:** status flips to `postponed`; game-monitor must mark the slate entry; detectors return None for non-`in_progress` games.
- **Missing `winprobability`:** some games lack it entirely → `win_prob_home=None`; swing/decided detectors return None; downstream skills already handle None (their notes require it).
- **Exhibitions:** NBA Summer League and All-Star events excluded per league-config season windows — scoreboard entries outside the configured windows are dropped with a debug log.
- **Score as string** (`"9"`): parse int; empty string → 0 only when status is `scheduled`, else `EspnParseError`.
- **`tiePercentage` > 0:** possible in NFL; `win_prob_home` remains the home figure; skills comparing to two-way Kalshi markets must note ties break the complement assumption — surfaced as `SwingEvent.tie_prob` and a `tie_risk` flag on GameDetail when > 0.01.

## Dependencies
vault (league-config reads). Called by: game-monitor agent, league-matching (start times/team abbrs), postmortem (final states), skill-matcher (condition tags derive from GameState).

## Testing requirements
- Parser fixtures: captured live JSON for MLB (in-progress — use the 2026-07-17 capture), plus NFL/NBA captures gathered in Phase 3 preseason; assert exact GameState fields including MLB inning/half mapping.
- Status normalization: table test incl. unknown status → `suspended`.
- Swing detector: synthetic buffers — gradual drift (no fire), 15pt jump in 3 min (fire), 15pt jump over 11 min (no fire), None gaps.
- Decided detector: one fixture per league rule branch, incl. win-prob-high-but-lead-condition-fails (no fire).
- Injury diffing: OUT appears, QUESTIONABLE→OUT, MLB starter exit in 4th (fire) vs 6th (no fire).
- Staleness: failure does not re-serve cached data.

## New types
```python
@dataclass
class SwingEvent:
    espn_event_id: str; direction: Literal["home", "away"]
    magnitude: float; window_s: int
    from_prob: Prob; to_prob: Prob; tie_prob: Prob; detected_at: datetime

@dataclass
class DecidedEvent:
    espn_event_id: str; leader: Literal["home", "away"]
    win_prob: Prob; rule: str          # which league branch fired, for audit
    detected_at: datetime

@dataclass
class GameDetail:      # GameState + summary extras
    state: GameState
    win_prob_series_len: int
    tie_risk: bool
    starting_pitcher_ids: dict | None  # MLB: {"home": id, "away": id}
```
