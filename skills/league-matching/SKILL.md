# league-matching

**Trigger:** the daily slate is being built, or any component holds an ESPN game and needs the corresponding Kalshi market (or vice versa).

## What this is for

Entity resolution between the two worlds: ESPN game IDs on one side, Kalshi market tickers on the other, using the alias map in `league-config.md` (read through the vault skill). Its cardinal rule is inherited from CONTRACTS.md rule 4: **ambiguity returns `None`, never a guess** — an unmatched game is simply not traded, which costs opportunity; a mismatched game trades the wrong outcome, which costs money.

## Interface

```python
resolve(game: GameState) -> MatchResult              # raises MatchingError (infra only, never for ambiguity)
resolve_slate(league: LeagueId, day: date) -> dict[str, MatchResult]  # espn_event_id -> result
parse_event_ticker(event_ticker: str, league: LeagueId) -> ParsedTicker  # raises TickerGrammarError
invalidate(league: LeagueId, day: date) -> None      # slate changed (postponement)
```

Exceptions: `MatchingError` (base, infra failures), `TickerGrammarError` (ticker doesn't parse — a grammar drift alarm, not a skip).

## Behavior

### Ticker grammar (MLB verified live 2026-07-17; NFL/NBA unverified — see rule 7)
1. Event ticker: `{SERIES}-{YY}{MON}{DD}{HHMM}{AWAY}{HOME}` — e.g. `KXMLBGAME-26JUL191920LADNYY` = LAD @ NYY, 2026-07-19, 19:20 **ET**. Market ticker: event ticker + `-{TEAM}` where TEAM is the YES side (`...-NYY` → YES = Yankees win). Team fields are variable-length Kalshi abbreviations; parse by anchoring the 9-char date-time block (`26JUL1919 20` → `YYMONDDHHMM`) and matching the trailing team string against the league's known Kalshi abbreviations, **longest-suffix-first** (HOME is the suffix, AWAY is what remains). If the remainder doesn't split into exactly two known abbreviations → `TickerGrammarError`.
2. `ParsedTicker = {series_ticker, away_kalshi_abbr, home_kalshi_abbr, start_time_et: datetime, yes_team_kalshi_abbr | None}`. Convert ET → UTC using `America/New_York` (DST-aware).

### Matching algorithm (numbered; `resolve`)
3. Resolve both ESPN team abbrs of `game` to canonical alias rows (league-config, via vault). Failure to resolve an ESPN abbr is a config gap → `MatchingError` (loud, because ESPN abbrs are supposed to be complete).
4. Candidate set: `kalshi-client.get_markets(series_ticker, status="open", date=game date ±1 day)` for the league's series ticker; parse each event ticker (rule 1); keep candidates whose two teams' alias rows equal the game's two alias rows (order-aware: away/home must match, not just the pair — home-team confusion is a real settlement difference for... nothing on a winner market, but order is checked anyway as a grammar sanity signal; order mismatch with team-set match logs a warning and still qualifies).
5. Exactly one candidate → `MatchResult(market=..., method="alias_exact", ambiguous=False)`.
6. Multiple candidates (**MLB doubleheaders — the canonical case**): tie-break on start time. Compute `|parsed start_time − game.start_time|`; a candidate qualifies if within `TIE_BREAK_WINDOW_MIN=45` minutes; if exactly one qualifies AND it is the unique closest → `method="alias_plus_start_time"`. Two candidates both within window (twin-bill games listed with identical placeholder times) → `MatchResult(market=None, ambiguous=True, method="none")`. Zero candidates → `market=None, ambiguous=False`.
7. **NFL/NBA grammar verification gate:** league-config marks non-MLB Kalshi abbrs/series tickers ⚠-unverified. Until a league's `grammar_verified: true` flag is set in league-config (a Phase 3 task per league: pull live markets, confirm grammar + abbr table, flip the flag), `resolve` for that league returns `market=None` with a `grammar-unverified` note rather than trusting guesses. Matching NEVER runs on an unverified abbreviation table.

### Alias-map edge handling (mandatory cases from league-config)
8. WSH/WAS: MLB verified Kalshi=WSH; the alias row is keyed by ESPN abbr, so Kalshi-side variations only affect ticker parsing — parsing uses the Kalshi-abbr column, and any live ticker containing an abbreviation absent from that column raises `TickerGrammarError` (which is the alarm that the table is wrong — exactly what we want, loudly, instead of a silent non-match).
9. Cross-league nickname collisions (Giants, Cardinals, Rangers, Bucs, Sox): structurally prevented — resolution is always league-scoped and abbreviation-based; nicknames never enter this skill.

### Caching
10. `resolve_slate` computes once per (league, day) and caches (in-memory, no TTL — matches are stable for the day). `invalidate(league, day)` on postponement/suspension (game-monitor calls it when a slate game's status flips). Individual `resolve` calls hit the slate cache.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `TIE_BREAK_WINDOW_MIN` | 45 | doubleheader disambiguation |
| `CANDIDATE_DATE_SPAN_DAYS` | ±1 | UTC/ET date-line straddling |

## Edge cases
- **Doubleheader, both games matched:** two ESPN events, two Kalshi events; each resolves via start-time proximity; the *pair* must be internally consistent (no market assigned to two ESPN events — assert, else both → None + alert).
- **Postponed game with market still open:** Kalshi's rules keep the market open ≤2 days (verified in `rules_secondary`); the ESPN event moves. The old match is invalidated; the resolver re-matches on the new date within `CANDIDATE_DATE_SPAN_DAYS` of the market's embedded start (which reflects the *original* schedule — hence matching tolerates date drift by searching market listings by close_ts, not ticker date, for postponed statuses).
- **Exhibition/unlisted games:** ESPN has games Kalshi doesn't list → `market=None, ambiguous=False` (normal, silent).
- **Ticker date in ET vs ESPN date in UTC:** a 10:05 PM ET game is next-day UTC; all comparisons in UTC after conversion (rule 2), candidate listing spans ±1 day.
- **Series ticker missing/renamed:** empty candidate sets across a whole slate day for an in-season league → escalate (Discord alert) rather than silently trading nothing.

## Dependencies
vault (league-config), kalshi-client (market listings), espn-data (GameState inputs). Called by: game-monitor (slate build), trader (re-verification before entry), postmortem (trade↔game joins).

## Testing requirements
Fixture set of real games with known correct matches, including the deliberately ambiguous — each fixture named:
- `fixture_mlb_normal`: 2026-07-19 LAD@NYY ↔ `KXMLBGAME-26JUL191920LADNYY` (captured live).
- `fixture_mlb_doubleheader`: same-day same-teams twin bill with distinct HHMM blocks → both matched via start-time; variant with identical HHMM → both None/ambiguous.
- `fixture_wsh`: WSH Nationals game ↔ ticker containing `WSH` (captured: `KXMLBGAME-26JUL191605WSHATH`).
- `fixture_ath`: Athletics under ATH; assert legacy OAK in a synthetic ticker raises `TickerGrammarError`.
- `fixture_unmatched_exhibition`: ESPN event with no Kalshi market → None, not ambiguous.
- `fixture_unverified_league`: NFL resolve before grammar flag → None + grammar-unverified.
- `fixture_et_utc_rollover`: 22:05 ET game matching across the UTC date line.
- Parser: longest-suffix-first splitting incl. ambiguous-prefix abbrs; ET→UTC DST both directions.

## New types
```python
@dataclass
class ParsedTicker:
    series_ticker: str; away_kalshi_abbr: str; home_kalshi_abbr: str
    start_time: datetime          # UTC, converted from embedded ET
    yes_team_kalshi_abbr: str | None  # market tickers only
```
