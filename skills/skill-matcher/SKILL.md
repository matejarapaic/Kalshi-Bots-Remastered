# skill-matcher

**Trigger:** the trader received a `CandidateSignal` and must decide which confirmed trading skill (if any) governs it.

## What this is for

The librarian between signals and the skill library. It queries `02-trading-skills/` through the vault skill (tag-filtered frontmatter query — never full-text, never direct disk), applies hard gates, and produces a **deterministic, auditable** fit score per skill. No LLM calls, no vibes: the same inputs always yield the same scores, and every score decomposes into named components a postmortem can audit.

## Interface

```python
match(signal: CandidateSignal, game: GameState,
      orderbook: OrderbookSnapshot | None = None,
      consensus: ConsensusOdds | None = None) -> list[SkillMatch]   # sorted by score desc
derive_condition_tags(game: GameState, signal: CandidateSignal) -> list[str]
```

Exceptions: `SkillMatcherError` (vault unavailable — the cycle-level "no data, no trades" case).

## Behavior

### Hard gates (in order; failing any gate excludes the skill from scoring entirely)
1. **Status:** `query(VaultQuery(directory="02-trading-skills", frontmatter_filters={"status": "confirmed"}))` — draft and retired skills are invisible here. **Safety invariant, restated: a `draft` skill can never reach sizing or execution because it never exits this gate.**
2. **League:** `game.league ∈ note.sports` (or `sports == [all]`).
3. **Signal-type mapping** (explicit table; a skill only sees its own signal family):

| SignalType | Skill |
|---|---|
| `overreaction-candidate` | live-win-prob-overreaction |
| `divergence-candidate` | sportsbook-kalshi-divergence |
| `injury-candidate` | injury-news-repricing-lag |
| `garbage-time-candidate` | garbage-time-mispricing |
| `game-final` | (never matches a trading skill; postmortem trigger) |

New skills extend this table via a `signal_types: [...]` frontmatter field; the four confirmed notes are grandfathered into the table above until they carry the field.
4. **Condition-tag overlap:** at least one tag of `note.market_conditions` present in `derive_condition_tags(game, signal)`.

### Condition-tag derivation (each tag has one precise rule)
| Tag | Rule |
|---|---|
| `live` | `game.status == in_progress` |
| `pregame` | `status == scheduled` and `start_time − now ≤ 60 min` |
| `endgame` | NFL/NBA: period ≥ 4 and `clock_seconds ≤ 300`; MLB: period ≥ 8 |
| `blowout` | score margin ≥ 15 (NBA), ≥ 17 (NFL), ≥ 5 (MLB) |
| `high-volatility` | any `SwingEvent` in signal payload with magnitude ≥ 0.10 |
| `momentum-swing` | signal payload carries a `SwingEvent` (any magnitude ≥ SWING_PTS) |
| `news-event` | signal payload carries an `InjuryEvent` |

### Scoring (deterministic formula)
5. `score = W_SIGNAL·s_signal + W_TAGS·s_tags + W_FRESH·s_fresh + W_HIST·s_hist`, weights in Configuration, summing to 1.0.
   - `s_signal` = 1.0 (the mapping table is exact; retained as a component so future multi-signal skills can score partial matches).
   - `s_tags` = |note.market_conditions ∩ derived tags| / |note.market_conditions|.
   - `s_fresh` = 1.0 if every input's `fetched_at` is within its skill-note staleness bound (90s overreaction / 60s divergence / per-note), linearly decaying to 0.0 at 2× the bound; missing optional inputs (orderbook/consensus not supplied) score 1.0 — absence is the trader's concern at entry-verification, not staleness.
   - `s_hist` = win-rate factor with low-sample neutral prior: `sample_size < 20 → 0.5`; else `clamp(win_rate, 0.2, 0.8)` rescaled to [0,1] via `(x − 0.2)/0.6`. Demo-labeled stats count (env-labeled fields, per postmortem spec).
6. `passed = score ≥ note.confidence_threshold` (the frontmatter value — **owner-confirmed Category B**; the weights are tuning parameters, Category A, but thresholds are not touchable here).
7. `reasons[]`: one line per component with its value and weight (`"s_tags=0.67 (2/3 tags: live, endgame) × 0.25"`) plus one line per hard gate passed. This is what makes postmortem threshold-review audits possible.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `W_SIGNAL` | 0.40 | |
| `W_TAGS` | 0.25 | |
| `W_FRESH` | 0.20 | |
| `W_HIST` | 0.15 | |
| low-sample prior | 0.5 below sample_size 20 | mirrors postmortem review rule |

Weights are Category A tuning values; changing them re-calibrates scores against owner-confirmed thresholds, so any change must be logged in the vault (00-meta changelog note) and mentioned at the next checkpoint.

## Edge cases
- **No skills match:** empty list — a normal result, not an error; trader declines the signal (and game-monitor's candidate still gets postmortem counterfactual treatment).
- **Two skills match one signal:** both returned, sorted; trader takes the highest-scoring `passed` skill. One-position-per-market-per-skill is the **trader's** rule, not this skill's — matcher output is advice.
- **Vault cache miss/stale → `SkillMatcherError`:** no skills readable = no matches = no entries this cycle (fail-closed), escalate if persistent.
- **Malformed skill note** (bad frontmatter): the vault skill's query already skips it with a warning; matcher additionally alerts if the count of confirmed skills drops below the last-known count (a skill silently vanishing from the library is an incident, not a quiet day).
- **Missing `win_rate/sample_size`** (nulls on a fresh note): treated as sample_size 0 → neutral prior.
- **`confidence_threshold` outside [0,1]:** vault schema validation should have caught it; if seen anyway, exclude the skill and alert (never clamp a trading threshold).

## Dependencies
vault (skill library queries), espn-data (GameState/SwingEvent/InjuryEvent shapes for tag derivation). Called by: trader (per signal). Does NOT call kalshi-client or odds-api — the trader supplies snapshots it already holds.

## Testing requirements
- Gate tests: draft skill invisible; league scoping; each SignalType row; tag-overlap gate.
- Tag derivation: one fixture per tag rule, per league where league-dependent (blowout margins ×3 leagues, endgame ×2 rules).
- Scoring determinism: same-input repeatability; component decomposition sums to reported score; each component's boundary values (fresh exactly at bound / at 2× bound; sample_size 19 vs 20).
- Threshold pass/fail exactly at `score == confidence_threshold` (passes — `≥`).
- reasons[] audit: every component present and parseable.

## New types
None beyond CONTRACTS.md (`SkillMatch`).
