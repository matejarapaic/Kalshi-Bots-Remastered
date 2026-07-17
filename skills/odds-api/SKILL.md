# odds-api

**Trigger:** the divergence or injury skill (via trader/game-monitor) needs the sportsbook consensus probability for a game; postmortem needs the closing consensus.

## What this is for

Fetches head-to-head odds from multiple sportsbooks via **The Odds API** (api.the-odds-api.com, v4 — verified 2026-07-17) and converts them into a single de-vigged consensus probability (`ConsensusOdds`). This spec writes the odds→probability math out in full because a silent conversion error here poisons every divergence trade.

## Interface

```python
get_consensus(league: LeagueId, home: TeamRef, away: TeamRef,
              live: bool) -> ConsensusOdds        # raises OddsApiError, OddsTeamResolutionError
american_to_prob(odds: int) -> Prob               # raises ValueError on odds in (-100, 100)
decimal_to_prob(d: float) -> Prob                 # raises ValueError on d <= 1.0
devig_two_way(p_a: Prob, p_b: Prob) -> tuple[Prob, Prob]
quota_remaining() -> int | None                   # from last response headers
```

Exceptions: `OddsApiError` (base), `OddsTeamResolutionError`, `OddsQuotaExceeded`, `OddsFormatError`.

## Behavior

### Fetch (verified against v4 docs)
1. Endpoint: `GET https://api.the-odds-api.com/v4/sports/{sport_key}/odds?regions=us&markets=h2h&oddsFormat=american&apiKey={ODDS_API_KEY}`. Sport keys from league-config mapping: nfl → `americanfootball_nfl`, nba → `basketball_nba`, mlb → `baseball_mlb`.
2. **`oddsFormat=american` is always sent explicitly and the parser assumes ONLY that format.** Format autodetection is forbidden: decimal 1.91 misread as American +191 flips a 52% into a 34% silently. If any `price` fails American-format validation (integer, |price| ≥ 100), raise `OddsFormatError` for the whole response — never skip-and-continue on format doubt.
3. Response shape: `[{id, sport_key, commence_time, home_team, away_team, bookmakers: [{key, title, last_update, markets: [{key: "h2h", outcomes: [{name, price}]}]}]}]`. `last_update` per bookmaker → `BookQuote.source_ts`.
4. Match the request's game by team resolution (rule 8) + `commence_time` within ±6h of the ESPN start (handles doubleheaders: closest `commence_time` wins; two candidates equally close → `OddsApiError`, no guess).

### THE MATH (worked, mandatory reading for the implementer)
5. **American → implied probability.**
   Positive odds +A (A ≥ 100): `p = 100 / (A + 100)`. Example: +150 → 100/250 = **0.400**.
   Negative odds −A (A ≥ 100): `p = A / (A + 100)`. Example: −150 → 150/250 = **0.600**.
   Odds in the open interval (−100, 100) are invalid American odds → `ValueError`.
   (Decimal, for completeness/tests only: `p = 1/D`; D=2.50 → **0.400**. `decimal_to_prob` exists for fixtures but the live path never uses it — rule 2.)
6. **Two-way de-vig (multiplicative normalization).** A book's implied probs sum to >1; the excess is the vig. `p_home_devig = p_home / (p_home + p_away)`, `p_away_devig = 1 − p_home_devig`. Example: −150/+130 → 0.600/0.435 (sum 1.035, 3.5% vig) → 0.600/1.035 = **0.580**. Power and Shin de-vig methods exist and are deliberately out of scope (documented so nobody "improves" this silently).
7. **Consensus.** De-vig **per book first**, then aggregate: `devigged_home_prob = mean(per-book de-vigged home probs)`; `max_pairwise_disagreement = max |p_i − p_j|` over de-vigged book probs; `book_count` = number of books contributing a valid two-way h2h quote. `book_count ≥ 3` is the divergence skill's validity gate — this skill returns the object regardless, with the fields set; consumers gate on them. Books listed in `EXCLUDED_BOOKS` (e.g. exchanges quoting from Kalshi itself — circularity) are dropped before counting.

### Team resolution
8. The Odds API returns full names ("Kansas City Chiefs"). Resolve via league-config alias maps **through the vault skill**: full-name match against `ESPN display name` and `Common names` columns within the requested league only. Unresolvable → `OddsTeamResolutionError` naming the string. **No fuzzy matching** — a new/renamed team is a config edit, not a guess.

### Quota (documented reality)
9. Paid tiers meter monthly requests; each odds call costs `regions × markets` credits. Response headers `x-requests-remaining` / `x-requests-used` → `quota_remaining()`. When remaining < `QUOTA_RESERVE`, live polling must degrade to `LIVE_POLL_DEGRADED_S` and log loudly; at 0, raise `OddsQuotaExceeded` (divergence skill simply stops finding entries — fail-closed). **Category B flag (cost, owner decision): which paid tier to buy.** At one 30s live poll per league-hour of play, MLB alone is ~120 requests/game — a 20K/month tier supports roughly 5 concurrent games at 30s cadence through a month; pick tier once real cadence is known.
10. HTTP 429 → backoff (base 2s, ×2, max 60s, 3 tries) then `OddsApiError`.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `ODDS_API_KEY` | — | env var, required |
| `ODDS_REGIONS` | `us` | request param |
| `ODDS_LIVE_POLL_S` | 30 | consumed by orchestrator cadence |
| `LIVE_POLL_DEGRADED_S` | 120 | quota-pressure fallback |
| `QUOTA_RESERVE` | 500 | credits held back for exits/postmortems |
| `EXCLUDED_BOOKS` | `[]` | populate if a book proves circular/stale |
| `COMMENCE_MATCH_WINDOW_H` | 6 | game matching window |

## Edge cases
- **3-way / tie-inclusive markets:** only `h2h` with exactly 2 outcomes both resolving to the requested teams is used; anything else (h2h_3_way, outrights) is excluded from that book's contribution.
- **Frozen in-play odds** (reviews, pitching changes): a book whose `last_update` is older than `STALE_BOOK_S=90` during a live game is dropped from consensus (its quote is a museum piece); if that drops `book_count` below 3, so be it — consumers gate.
- **Missing books mid-game:** books pull markets during volatility; `book_count` reflects contributors only. Never backfill from a previous poll.
- **Both teams resolve but reversed** (API home ≠ ESPN home, neutral-site quirks): resolution is by team identity, not order; `devigged_home_prob` always refers to OUR `home` argument.
- **Duplicate outcomes or |price| < 100 in payload:** `OddsFormatError` for that book, excluded; whole-response only if ALL books malformed.
- **Empty response** (no books quoting): return ConsensusOdds with `book_count=0` — an answer, not an error.

## Dependencies
vault (league-config alias maps). Called by: trader (divergence/injury entry checks), game-monitor (divergence-candidate detection), postmortem (closing consensus counterfactuals).

## Testing requirements
- Conversion tables: (+150, −150, +100, −100, +9900, −9900) → exact probs; ValueError inside (−100, 100); decimal round-trips (D=2.5 ↔ +150 territory).
- De-vig fixtures: −150/+130 → 0.580 (worked example above); symmetric −110/−110 → 0.500; extreme −10000/+2500.
- Consensus: 3 books with known disagreement → exact mean and max_pairwise; stale-book exclusion; book_count<3 passthrough.
- Team resolution: "Kansas City Chiefs" → KC; "St. Louis Cardinals" resolves in MLB but NOT via NFL Cardinals (league scoping); unknown name raises.
- Format guard: decimal-shaped payload (prices like 1.91) → `OddsFormatError`, no partial parse.

## New types
None beyond CONTRACTS.md (`ConsensusOdds`, `BookQuote`).
