# skill-matcher

**Trigger:** the trader received a `CryptoSignal` and must decide which confirmed trading skill (if any) governs it.

## What this is for

The librarian between signals and the skill library. It queries `02-trading-skills/` through the vault skill (tag-filtered frontmatter query — never full-text, never direct disk), applies hard gates, and produces a **deterministic, auditable** fit score per skill. No LLM calls, no vibes: the same inputs always yield the same scores, and every score decomposes into named components a postmortem can audit.

## Interface

```python
class SkillMatcher:
    def __init__(self, vault: Vault): ...
    def match(self, signal: CryptoSignal,
              orderbook: OrderbookSnapshot | None = None,
              now: datetime | None = None) -> list[SkillMatch]   # sorted by score desc

derive_condition_tags(signal: CryptoSignal, now: datetime | None = None) -> list[str]
```

Exceptions: `SkillMatcherError` (vault unavailable — the cycle-level "no data, no trades" case).

## Behavior

### Hard gates (in order; failing any gate excludes the skill from scoring entirely)
1. **Lifecycle short-circuit:** `window-open`, `phase-change`, and `window-close` signals never match a trading skill — `match()` returns `[]` before the vault is even queried. Lifecycle signals drive orchestration (phase bookkeeping; `window-close` triggers postmortems), not entries. Only candidate signal types (currently `fair-value-candidate`) reach the gates below.
2. **Status:** notes are queried without a status filter and kept only when `status ∈ allowed_statuses` (constructor parameter, default `("confirmed",)`); `_skill-template.md` is always excluded. The trader passes `("confirmed", "draft")` on `env=demo` so draft skills can trade paper/demo and accumulate `demo_*` stats toward confirmation. **Safety invariant, restated for the crypto era: a `draft` skill can never trade live money — the live-mode matcher stays confirmed-only AND the sprint-5 live guard re-asserts `confirmed` status independently. Retired skills are invisible everywhere.**
3. **Signal-type declaration:** `signal.signal_type ∈ note.signal_types` — declared per note in frontmatter, no hardcoded mapping table remains. A note with a missing or empty `signal_types` field matches nothing (fail-closed).
4. **Family:** `signal.series_ticker ∈ note.families`, or `families` contains the `all` wildcard. A note with a missing or empty `families` field matches nothing (fail-closed).
5. **Condition-tag overlap:** at least one tag of `note.market_conditions` present in `derive_condition_tags(signal)`.
6. **Threshold sanity:** `note.confidence_threshold` must be numeric and within [0,1]; otherwise the skill is excluded and an alert logged — **never clamp a trading threshold** (see Edge cases).

### Condition-tag derivation (each tag has one precise rule)
| Tag | Rule |
|---|---|
| `live` | always — the markets trade 24/7, so every signal is live |
| phase tag (`opening` / `midpoint` / `near_close` / `settled`) | `signal.phase`, verbatim, whenever set |
| `high-volatility` | `payload["sigma"] ≥ 1.0` (annualized realized vol ≥ 100%) |
| `thin-book` | `payload["thin_book"]` is truthy |

### Scoring (deterministic formula)
7. `score = W_SIGNAL·s_signal + W_TAGS·s_tags + W_FRESH·s_fresh + W_HIST·s_hist`, weights in Configuration, summing to 1.0.
   - `s_signal` = 1.0 (the declared-type gate is exact; retained as a component so future multi-signal skills can score partial matches).
   - `s_tags` = |note.market_conditions ∩ derived tags| / |note.market_conditions|.
   - `s_fresh` = 1.0 if the worst input age — `max(now − signal.emitted_at, now − orderbook.fetched_at)` when an orderbook is supplied, signal age alone otherwise — is within the staleness bound, linearly decaying to 0.0 at 2× the bound. The bound is the note's `staleness_bound_s` frontmatter field, defaulting to `DEFAULT_STALENESS_BOUND_S` (10 s — streaming inputs age in seconds, not minutes). A missing orderbook contributes no age and does not penalize — absence is the trader's concern at entry-verification, not staleness.
   - `s_hist` = win-rate factor with low-sample neutral prior. Env-labeled stats take precedence: when `demo_sample_size` is set, use `demo_win_rate`/`demo_sample_size`; otherwise `win_rate`/`sample_size` (per postmortem spec). `sample_size < 20` or missing win rate → 0.5; else `clamp(win_rate, 0.2, 0.8)` rescaled to [0,1] via `(x − 0.2)/0.6`.
8. `passed = score ≥ note.confidence_threshold` (the frontmatter value — **owner-confirmed Category B**; the weights are tuning parameters, Category A, but thresholds are not touchable here).
9. `reasons[]`: one line per hard gate passed (`gate:signal_type …`, `gate:family …`, `gate:tags overlap […]`) plus one line per component with its value and weight (`"s_tags=0.67 (2/3 tags: live, midpoint) x 0.25"`). This is what makes postmortem threshold-review audits possible.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `W_SIGNAL` | 0.40 | Category A tuning; carried unchanged from the previous build |
| `W_TAGS` | 0.25 | Category A tuning; carried unchanged |
| `W_FRESH` | 0.20 | Category A tuning; carried unchanged |
| `W_HIST` | 0.15 | Category A tuning; carried unchanged |
| `LOW_SAMPLE` | neutral prior 0.5 below sample_size 20 | mirrors postmortem review rule; carried unchanged |
| `DEFAULT_STALENESS_BOUND_S` | 10 s | PROPOSED 2026-07-22 (crypto pivot default, pending owner confirmation); overridable per note via `staleness_bound_s` |
| `HIGH_VOL_SIGMA` | 1.0 | PROPOSED 2026-07-22 (crypto pivot default, pending owner confirmation) |

Weights are Category A tuning values; changing them re-calibrates scores against owner-confirmed thresholds, so any change must be logged in the vault (00-meta changelog note) and mentioned at the next checkpoint.

## Edge cases
- **No skills match:** empty list — a normal result, not an error; trader declines the signal (declined candidates still get postmortem counterfactual treatment when `window-close` drives the audit). Note the trader currently declines *all* candidates until sprint-3 wires the fair-value model — matcher output is advice either way.
- **Two skills match one signal:** both returned, sorted; trader takes the highest-scoring `passed` skill. One-position-per-market-per-skill is the **trader's** rule, not this skill's — matcher output is advice.
- **Vault cache miss/stale → `SkillMatcherError`:** no skills readable = no matches = no entries this cycle (fail-closed), escalate if persistent.
- **Malformed skill note** (bad frontmatter): the vault skill's query already skips it with a warning; matcher additionally alerts if the count of confirmed skills drops below the last-known count (a skill silently vanishing from the library is an incident, not a quiet day).
- **Missing `win_rate/sample_size`** (nulls on a fresh note): treated as sample_size 0 → neutral prior.
- **`confidence_threshold` non-numeric or outside [0,1]:** the vault's write-time schema check catches this on writes; the matcher's own guard covers notes hand-edited on disk — exclude the skill and alert (never clamp a trading threshold).
- **`settled`-phase candidates:** the phase tag is emitted verbatim, `settled` included; only a note that explicitly lists `settled` in `market_conditions` can match one.

## Dependencies
vault (skill library queries), window-monitor (CryptoSignal/WindowRef shapes). Called by: trader (per candidate signal). Does NOT call kalshi-client or crypto-price-feed — the trader supplies the orderbook snapshot it already holds.

## Testing requirements
- Gate tests: draft skill invisible; each lifecycle signal type returns `[]`; `signal_types` declaration gate; `families` gate including the `all` wildcard; tag-overlap gate (phase mismatch); invalid `confidence_threshold` excluded (written around the vault's schema check to simulate a hand-edited note).
- Tag derivation: one fixture per tag rule — always-live plus phase; `high-volatility` on both sides of the sigma boundary; `thin-book`.
- Scoring determinism: same-input repeatability; component decomposition sums to reported score; freshness ordering across the decay curve (at bound / between bound and 2× / at 2× bound); env-labeled history stats (`demo_*`) beat the neutral prior; sample_size 19 vs 20.
- Threshold pass/fail exactly at `score == confidence_threshold` (passes — `≥`).
- reasons[] audit: gate lines and every component present and parseable.

## New types
None beyond CONTRACTS.md (`SkillMatch`).
