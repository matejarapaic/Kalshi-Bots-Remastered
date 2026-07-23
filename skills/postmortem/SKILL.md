# postmortem

**Trigger:** a `window-close` signal (window-monitor detects the active KXBTC15M window rolling over; one run per settled 15-minute window). Today the orchestrator routes `window-close` to `Analyst.on_window_close`, which is a logging stub — `run()` is complete and tested but automatic invocation at the 96-windows/day cadence is sprint-4 scope (see the last section).

## What this is for

The honest-feedback loop. After each window it audits every trade (did the recorded entry conditions actually hold? did exits deviate?), computes counterfactuals for every *declined* candidate signal, updates the skill notes' statistics, and writes the postmortem note a human reads before ever touching a threshold. It runs **off the live path** as a batch job — one of the few legitimate direct-vault-*read* contexts, but all *writes* still go through the vault skill so the cache never desynchronizes (stated per the vault skill's rule).

## Interface

```python
Postmortem(vault, kalshi_client, discord_bot=None, env: str = "demo")
run(family: str, event_id: str) -> PostmortemReport   # idempotent per settled window
```

`family` is the series ticker (e.g. `KXBTC15M`); `event_id` is the window's event ticker. Exceptions: `PostmortemError` (base), `SettlementMismatch` (CRITICAL — see rule 5).

## Behavior

### Inputs gathered
1. The window's active-window note (`03-market-context/active-windows/{event_id}.md`) — frontmatter is machine state (`market_ticker`, `strike`, `phase`, `spot`, `sigma`, …), body carries the `- SIGNAL {json}` log lines appended by window-monitor. All trade notes for the window: vault query on `04-trade-history/trades` filtered by `event_id` frontmatter. Settlement truth from `kalshi_client.get_settlements(ticker)` for every distinct `market_ticker` across the trade notes plus the window note.
2. Idempotency: an existing postmortem note with `settlement_status: settled` short-circuits `run` — the report is reconstructed from its frontmatter (violation/deviation counters and threshold flags are not persisted there and come back zeroed/empty). Any other status re-runs fully; new frontmatter merges over the existing note's.
3. Settlement pending: `get_settlements` returning empty (or raising — swallowed) for a ticker that still has a trade with `status != closed` → `settlement_status: pending`. The note is still written; a later `run` fills the gap. There is no in-process retry loop (`SETTLEMENT_WAIT_MAX_MIN` is declared but unused in v1 — re-runs are the retry mechanism).

### Settlement cross-check
4. A settlement `result == "void"` on any audited ticker sets `settlement_status: voided`.
5. Kalshi settlement result vs. the window log's own recorded outcome: when the window note carries both `yes_label` and `settled_direction` for its own `market_ticker`, expected = `yes` if `settled_direction == yes_label` else `no`; a differing Kalshi result sets `settlement_status: mismatch`, writes a `⚠ SETTLEMENT MISMATCH` block (the note is written *before* raising), posts a `critical` Discord notify if a bot is wired, and raises `SettlementMismatch`. **Never silently pass** — this either means a money bug or a voided/mispriced settlement, and a human looks either way. Caveat: nothing in production code stamps `settled_direction` yet — the cross-check is latent until sprint-4's BRTI-log paper settlement writes it.

### Per-trade audit (recorded per trade in the postmortem note)
6. **Entry-condition audit:** the trade note's `entry_conditions` frontmatter mapping (condition name → boolean, snapshotted at entry) is re-read; any `False` counts the trade as an entry violation and flags the line `⚠ ENTRY VIOLATION` (the trade may even have won — process and outcome are scored separately).
7. **Exit-rule compliance:** the trade note's recorded `exit_deviation` boolean (the trader records deviations at exit time) is counted into `exit_deviations`.
8. **Slippage:** `entry_price_cents − signal_price_cents`, in cents, when both are recorded; `None` otherwise.
9. **P&L:** the note's `realized_pnl_cents` when present (net of real fees — never the estimate). When absent and the trade's market settled, reconstructed from the note: `cost = contracts × entry_price_cents + fee_cents`; won = settlement result equals the trade's `side`; P&L = `contracts × 100 − cost` if won else `−cost`. Each audit row carries the trade note's `env` label (falling back to the skill's env).

### Declined-candidate counterfactuals
10. Every `- SIGNAL {json}` line in the window note's body that did NOT become a trade (its `id` matches no trade note's `signal_id`) and is not a `window-close` signal gets a counterfactual: when the line records an `entry_price_cents` and its ticker settled `yes`/`no` — entry at the recorded price, **100 contracts (normalized analysis size)**, entry fee via `est_fee_cents`, **held to settlement**. Spec deviation (flagged 2026-07-17, carried forward): v1 holds to settlement instead of replaying each skill's invalidation rules against the intra-window price log — the monitor's v1 log granularity is not sufficient for a faithful replay. The assumption line ("held to settlement, fill at recorded price, 100 contracts, no market impact") is stated on every declined entry. Signals without a price, or on voided/pending markets, are still listed with `counterfactual_pnl_cents: None`. Malformed JSON lines are skipped. The `declined_reason` field is read from the signal line when present (the trader's disposition strings; not yet wired into the log — reads `None` today). This is how thresholds get honest feedback — declines that keep "winning" counterfactually push thresholds down at review; declines that keep "losing" validate them.

### Skill stats — sole writer
11. Via `vault.update_frontmatter(path, updates, caller="analyst")` (the scope table admits only these fields), grouped per skill over this run's trades with a non-`None` P&L: `{prefix}sample_size += settled trades`, `{prefix}win_rate = (old_wr × old_n + wins) / new_n` (rounded to 4 dp), where `prefix` is `demo_` when `env == "demo"` and empty (the legacy prod pair) otherwise — env-suffixed fields keep demo results from contaminating prod stats while both stay visible. A "win" = P&L > 0 net of fees. A missing skill note skips that skill's update without failing the run.
12. **Threshold review flag:** when a skill's updated (env-appropriate) `sample_size ≥ 20` AND `|new win_rate − breakeven| ≥ 0.10`, where breakeven = mean `entry_price_cents` of *this window's* trades for that skill ÷ 100, write a `⚠ THRESHOLD REVIEW` line into the note's "Threshold review" section and the report's `threshold_flags`. **Humans change thresholds; this skill only surfaces evidence** — it never edits conditions, thresholds, or `status`.

### Outputs
13. `04-trade-history/postmortems/{utc-date}-{family}-{event_id}.md` (frontmatter: see New types). Body sections in order: `⚠ SETTLEMENT MISMATCH` (when rule 5 fires), `Threshold review` (when rule 12 fires), `Trades` (one line per audit: path, skill, P&L, slippage, violation flag — or "none (watched, nothing traded — that is data)"), `Declined candidates` (type, counterfactual P&L, assumption — or "none"). Zero trades and zero candidates still writes the minimal note (slate-coverage evidence). Re-runs merge frontmatter over an existing note at the same path.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `SETTLEMENT_WAIT_MAX_MIN` | 60 | CONFIRMED 2026-07-17 — declared; v1 has no in-process retry, pending notes are filled by re-runs |
| `REVIEW_MIN_SAMPLE` | 20 | CONFIRMED 2026-07-17 — mirrors analyst prompt |
| `REVIEW_DELTA` | 0.10 | CONFIRMED 2026-07-17 — win-rate divergence trigger |
| counterfactual size | 100 contracts | PROPOSED 2026-07-22 (crypto pivot default, pending owner confirmation) — normalized analysis-only size, inline in the counterfactual math |

## Edge cases
- **Window note missing/corrupt:** postmortem proceeds with trade notes + settlements as ground truth, logs `⚠ RECORD GAP` (a record-keeping bug is itself a finding); counterfactuals and the settlement cross-check are skipped (no signal log / no recorded outcome).
- **Voided market:** `settlement_status: voided`; counterfactuals for that ticker skipped (no true outcome). v1 caveat: per-trade P&L *reconstruction* (rule 9) does not special-case `void` — a trade lacking recorded `realized_pnl_cents` on a voided market reconstructs as a full loss of cost; trades with recorded P&L are unaffected. Flagged for sprint-4.
- **Multiple tickers in one event:** each trade is audited against its own market's settlement (`settled_results` is per ticker); event-level totals aggregate. The cross-check applies only to the window note's own `market_ticker`.
- **Re-run after UTC midnight:** the note path's date prefix comes from run time, not window close time — a pending window re-run the next UTC day writes a second note instead of filling the first (v1 caveat; matters near-midnight only).
- **No Discord bot wired:** a mismatch still writes the note and raises; only the notify is skipped.
- **Zero trades, zero candidates:** minimal note still written — "we watched, nothing fired" is data.

## Dependencies
vault (all reads on batch path + all writes), kalshi-client (`get_settlements`; `est_fee_cents` for counterfactual entry fees), window-monitor's active-window note format (frontmatter + `- SIGNAL {json}` log lines) as the input contract, discord-bot (optional; critical notify on mismatch). Runs under the analyst agent; never on the live path.

## Testing requirements
- Entry-audit fixtures: trade note with one condition false → violation flagged; all-true → clean settled win audited.
- Counterfactual engine: a declined signal line in the window log → known held-to-settlement P&L at 100 contracts with the assumption line present; `window-close` lines and traded `signal_id`s excluded.
- Stats math: demo trades update `demo_win_rate`/`demo_sample_size`; prod fields untouched; accumulation across successive runs is incremental-mean-correct; scope enforcement (attempt to write `confidence_threshold` raises in the vault skill).
- Threshold flag: sample below 20 → no flag; ≥ 20 with divergence ≥ 0.10 → flag; ≥ 20 with divergence < 0.10 → no flag.
- Settlement mismatch: fixture where Kalshi settles opposite of the window log's `settled_direction` → `SettlementMismatch` + critical notify, note written first.
- Pending settlement: empty settlements with an un-closed trade → `settlement_status: pending`, note written; a later run completes it.
- Zero trades → minimal note still written.
- Idempotency: second `run` on a settled window is a no-op returning the note-derived report.

## Sprint-4 adaptations (planned, not yet implemented)
- **Automatic invocation:** `Analyst.on_window_close` currently logs and returns; sprint-4 wires the actual `run()` call at the 96-windows/day cadence.
- **Batched Discord rollups** replacing per-window notifications (96 windows/day would flood a channel).
- **Daily aggregate postmortem notes** — a per-day rollup across all windows; threshold-review flags surface there as well as in per-window notes.
- **Crypto-native counterfactual decomposition:** model-was-right / vol-was-right / constituent-drift attribution replacing the v1 held-to-settlement assumption (requires denser intra-window price logging by the monitor).
- **Paper settlement from the window's BRTI log** — stamps `settled_direction` on active-window notes, activating the rule-5 cross-check for paper runs, and fixing the voided-market P&L reconstruction caveat.

## New types
```python
@dataclass
class PostmortemReport:
    family: str                    # series ticker, e.g. KXBTC15M
    event_id: str                  # event ticker of the audited window
    trades_audited: int; entry_violations: int; exit_deviations: int
    declined_candidates: int; counterfactual_pnl_cents: int
    realized_pnl_cents: int
    settlement_status: Literal["settled", "pending", "voided", "mismatch"]
    threshold_flags: list[str]; note_path: str
# Postmortem note frontmatter: {date, family, event_id, settlement_status,
#   realized_pnl_cents, counterfactual_pnl_cents, trades: int, declined: int, env}
```
