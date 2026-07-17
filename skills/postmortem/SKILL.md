# postmortem

**Trigger:** a `game-final` signal (ESPN `STATUS_FINAL` via espn-data). One run per finished game.

## What this is for

The honest-feedback loop. After each game it audits every trade (did the entry conditions actually hold? did exits follow the rules?), computes counterfactuals for every *declined* candidate, updates the skill notes' statistics, and writes the postmortem note a human reads before ever touching a threshold. It runs **off the live path** as a batch job — one of the few legitimate direct-vault-*read* contexts, but all *writes* still go through the vault skill so the cache never desynchronizes (stated per the vault skill's rule).

## Interface

```python
run(league: LeagueId, espn_event_id: str) -> PostmortemReport   # idempotent per game
```

Exceptions: `PostmortemError` (base), `SettlementMismatch` (CRITICAL — see rule 4).

## Behavior

### Inputs gathered
1. The game's active-game note (`03-market-context/active-games/{league}-{id}.md`) — final state + full signal log; all trade notes for the game (vault query on `04-trade-history/trades` by `espn_event_id` frontmatter); the risk ledger history; settlement truth from `kalshi-client.get_settlements(market_ticker)`.
2. Settlement wait: Kalshi settles minutes after ESPN final (`settlement_timer_seconds: 120` observed live). Retry `get_settlements` with backoff up to `SETTLEMENT_WAIT_MAX_MIN=60`; still unsettled → write a partial postmortem flagged `settlement-pending` and re-run on the next analyst cycle.
3. Idempotency: a completed postmortem note for the game short-circuits `run` (re-runs only fill `settlement-pending` gaps).

### Settlement cross-check
4. Kalshi settlement result vs. ESPN final winner (via league-matching's resolved market and `yes_team_kalshi_abbr`): mismatch raises `SettlementMismatch` and posts a `critical` Discord notify. **Never silently pass** — this either means a mismatched market traded (money bug) or a voided/fair-priced settlement (rain-shortened MLB — check `MarketRef.settlement_notes`), and a human looks either way.

### Per-trade audit (recorded per trade in the postmortem note)
5. **Entry-condition audit:** for each numbered entry condition in the skill's note, re-evaluate against the snapshot recorded in the trade note at entry time → boolean per condition. Any `False` = a process violation flagged `⚠ ENTRY VIOLATION` (the trade may even have won — process and outcome are scored separately).
6. **Exit-rule compliance:** which invalidation condition fired first (from the signal/price log) vs. what the trader actually did and when; deviations flagged.
7. **Slippage:** signal-time target price vs. actual `avg_fill_price`, in cents.
8. **P&L:** net of real fees (fills' `taker_fees_dollars` / positions' `fees_paid_dollars` — never the estimate); holding time; `KALSHI_ENV` label.

### Declined-candidate counterfactuals
9. Every candidate signal in the game's log that did NOT become a trade gets: the reason it was declined (matcher score below threshold / sizing zero with `capped_by` / approval rejected or expired / entry re-verification failed — all recoverable from logs), and a counterfactual result: entry at the snapshot's price, exit per the skill's invalidation rules evaluated against the rest of the game's price/win-prob log, settlement if held. Assumptions (fill at ask, no market impact) stated in the note. This is how thresholds get honest feedback — declines that keep "winning" counterfactually push thresholds down at review; declines that keep "losing" validate them.

### Skill stats — sole writer
10. Via `vault.update_frontmatter(path, updates, caller="analyst")` (the scope table admits only these fields): `demo_sample_size += settled trades`, `demo_win_rate = lifetime demo wins / demo_sample_size` (prod uses `win_rate`/`sample_size`; env from the trade notes' labels — Category A choice: env-suffixed fields keep demo results from contaminating prod stats while both stay visible). A "win" = P&L > 0 net of fees. The legacy `win_rate`/`sample_size` fields remain the prod pair per the template.
11. **Threshold review flag:** when a skill's (env-appropriate) `sample_size ≥ 20` AND `|observed win_rate − mean entry-price-implied breakeven|` ≥ 0.10, write a prominent `⚠ THRESHOLD REVIEW` block into this postmortem note AND the next daily-slate note, with the evidence table (trades, counterfactuals, binding caps). **Humans change thresholds; this skill only surfaces evidence** — it never edits conditions, thresholds, or `status`.

### Outputs
12. `04-trade-history/postmortems/{date}-{league}-{espn_event_id}.md` (frontmatter: New types below); per-trade updates appended to each trade note (settlement, final P&L, audit verdicts); daily-slate flag blocks when rule 11 fires.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `SETTLEMENT_WAIT_MAX_MIN` | 60 | retry window before `settlement-pending` |
| `REVIEW_MIN_SAMPLE` | 20 | mirrors analyst prompt |
| `REVIEW_DELTA` | 0.10 | win-rate divergence trigger |

## Edge cases
- **Game postponed after entries:** no `game-final` fires; a slate-sweep fallback (analyst nightly) postmortems any game with open positions and status `postponed` — positions carried per Kalshi's ≤2-day rule (from `settlement_notes`), note flagged `carried`.
- **Suspended, resumes tomorrow:** same path; postmortem waits for the true final.
- **Trade note missing/corrupt:** postmortem proceeds with ledger + fills as ground truth, flags `⚠ RECORD GAP` (a record-keeping bug is itself a finding).
- **Market voided / "fair price" settlement** (cancelled game): P&L computed from actual settlement revenue; counterfactuals skipped (no true outcome); note flagged `voided`.
- **Multiple markets one game** (both team-side markets traded): each trade audited against its own market's settlement; game-level totals aggregate.
- **Zero trades, zero candidates:** minimal note still written (slate coverage evidence — "we watched, nothing fired" is data).

## Dependencies
vault (all reads on batch path + all writes), espn-data (final state), kalshi-client (settlements, fills), league-matching (market↔game join). Runs under the analyst agent; never on the live path.

## Testing requirements
- Entry-audit fixtures: trade note with one condition false → violation flagged; all-true → clean.
- Counterfactual engine: declined divergence candidate replayed against a synthetic price log → known exit and P&L; assumption lines present.
- Stats math: 3 settled demo trades (2 wins) → `demo_win_rate=0.667`, `demo_sample_size=3`; prod fields untouched; scope enforcement (attempt to write `confidence_threshold` raises).
- Threshold flag: fixtures at sample 19 (no flag), 20 + delta 0.11 (flag), 20 + delta 0.09 (no flag).
- Settlement mismatch: fixture where Kalshi settles NO but ESPN winner implies YES → `SettlementMismatch` + critical notify.
- Idempotency: second `run` on a completed game is a no-op.

## New types
```python
@dataclass
class PostmortemReport:
    league: LeagueId; espn_event_id: str
    trades_audited: int; entry_violations: int; exit_deviations: int
    declined_candidates: int; counterfactual_pnl_cents: int
    realized_pnl_cents: int; settlement_status: Literal["settled", "pending", "voided", "mismatch"]
    threshold_flags: list[str]; note_path: str
# Postmortem note frontmatter: {date, league, espn_event_id, settlement_status,
#   realized_pnl_cents, counterfactual_pnl_cents, trades: int, declined: int, env}
```
