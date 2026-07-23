# postmortem

**Trigger:** analyst-driven, per settled 15-minute window (~96/day per family). The orchestrator routes each `window-close` signal to `Analyst.on_window_close`, which queues the window; `Analyst.poll_pending` (every orchestrator tick) polls `broker.get_market_raw(market_ticker)` — throttled to one REST check per `SETTLE_POLL_INTERVAL_S` (5s) per pending window — until the market carries a `result` in `yes`/`no`/`void` or a status in `finalized`/`settled`. A window still unfinalized `SETTLE_GIVE_UP_S` (900s) after close is an incident: the analyst stops polling, posts a Discord `warning`, and runs the postmortem anyway with `market_result=None` (pending report). Before calling `run()`, the analyst settles the paper broker from the real result and stamps `settled_result`/`expiration_value`/`settled_direction` (`up`/`down`) onto the active-window note.

## What this is for

The honest-feedback loop. After each window it audits every trade (did the recorded entry conditions actually hold? did exits deviate?), computes counterfactuals for every *declined* candidate signal plus the crypto-native counterfactual dimensions (model-was-right / vol-was-right / constituent-drift), and appends the window's section to a **daily aggregate note** a human reads before ever touching a threshold. It runs **off the live path** as a batch job — all writes go through the vault skill so the cache never desynchronizes. `run()` itself makes no network calls: settlement truth arrives as parameters from the analyst's poll.

## Interface

```python
Postmortem(vault, kalshi_client, discord_bot=None, env: str = "demo")
run(family: str, event_id: str,
    market_result: str | None = None,      # "yes"/"no"/"void"; None = pending
    expiration_value: float | None = None, # Kalshi's settlement print
    closes_at: datetime | None = None,     # dates the daily note (UTC)
    ) -> tuple[PostmortemReport, dict[str, list[dict]]]   # idempotent per window
update_skill_stats(outcomes_by_skill: dict[str, list[dict]]) -> list[str]  # flags
window_realized_vol(log_lines: list[dict]) -> float | None  # module-level helper
```

`family` is the series ticker (e.g. `KXBTC15M`); `event_id` the window's event ticker; the window's market ticker is derived as `{event_id}-{last two chars}` (grammar: the trailing `-MM` duplicates the close minute, e.g. `KXBTC15M-26JUL222130` → `…2130-30`). `run()` returns the report **plus** per-skill outcome rows — the caller batches those into `update_skill_stats()`; `run()` never touches skill stats. Exceptions: `PostmortemError` (base), `SettlementMismatch` (CRITICAL — see rule 4).

## Behavior

### Inputs gathered
1. `market_result` and `expiration_value` come from the analyst's settlement poll (see Trigger). The window's active-window note (`03-market-context/active-windows/{market_ticker}.md`) — frontmatter is machine state (`strike`, `phase`, …), body carries the `- SIGNAL {json}` and `- LOG {json}` lines appended by window-monitor. All trade notes for the window: vault query on `04-trade-history/trades` filtered by `event_id` frontmatter.
2. Idempotency: the daily aggregate note's frontmatter `settled_events` list. An `event_id` already present short-circuits `run` with a replay report (`settlement_status: settled`, counters zeroed) and empty outcomes.
3. `settlement_status`: `void` → `voided`; anything other than `yes`/`no` → `pending`; else `settled`.

### Settlement cross-check
4. The direction implied by `expiration_value` vs the window note's `strike` (`yes` iff expiration ≥ strike) must agree with Kalshi's result. Disagreement → `settlement_status: mismatch`, a `**⚠ SETTLEMENT MISMATCH**` block in the window's section (the note is written *before* raising), a `critical` Discord notify if a bot is wired, and `SettlementMismatch` raised. **Never silently pass** — this either means a money bug or a mispriced settlement, and a human looks either way. Because the raise happens after the note write, a mismatched window is in `settled_events` and will not re-run: mismatch is investigated, not retried, and its trade outcomes never reach skill stats. Cross-checking against the system's own BRTI stream remains future work — today's check is internal consistency of Kalshi's two fields.

### Per-trade audit (one line per trade in the window's section)
5. **Entry-condition audit:** the trade note's `entry_conditions` frontmatter mapping (condition name → boolean, snapshotted at entry) is re-read; any `False` counts the trade as an entry violation and flags the line `⚠ ENTRY VIOLATION` (the trade may even have won — process and outcome are scored separately).
6. **Exit-rule compliance:** the trade note's recorded `exit_deviation` boolean is counted into `exit_deviations`.
7. **Slippage:** `entry_price_cents − signal_price_cents`, in cents, when both are recorded; `None` otherwise.
8. **P&L:** the note's `realized_pnl_cents` when present (net of real fees — never the estimate). A trade still open at settlement (status `settled`/`mismatch` only) is **closed by `run()`**: P&L derived (`cost = contracts × entry_price_cents + fee_cents`; won = result equals the trade's `side`; `contracts × 100 − cost` if won else `−cost`) and written back with `status: closed`, `exit_reason: held_to_settlement`.
9. **Model-was-right:** did the entered `side` match the settled direction? Counted per window into `model_direction_hits` and marked `model✓`/`model✗` per line. Per-window this is coin-flippy — it exists to be judged in the aggregate, and the narrative says so.
10. **Vol-was-right:** `window_realized_vol()` computes annualized realized vol from the window note's `- LOG ` spot samples (sqrt(dt)-normalized log returns, annualized on a `SECONDS_PER_YEAR = 31_536_000` base — same normalization as the live estimator; needs ≥ 5 usable points, else `None`; ~30s cadence, so indicative, not authoritative). Ratio = realized vol ÷ mean `sigma` recorded on this window's trade notes; a ratio outside `VOL_RATIO_BAND (0.5, 2.0)` writes a `⚠ VOL-WAS-WRONG` flag into the section and the report's `threshold_flags`.
11. **Constituent drift:** any `- LOG ` line with `healthy < total` (a spot-feed constituent degraded mid-window) sets `constituent_drift`, writes an exclusion line, and marks every outcome row from this window `excluded` — the window is dropped from aggregate learning.

### Declined-candidate counterfactuals
12. Every `- SIGNAL {json}` line of type `fair-value-candidate` whose `id` matches no trade note's `signal_id` gets a counterfactual: entry at the recorded `entry_price_cents`, **`CF_CONTRACTS` = 100 (normalized analysis size)**, entry fee via `est_fee_cents`, **held to settlement**. Carried from v1 with the same caveat: holding to settlement instead of replaying invalidation rules against the intra-window log is a deliberate deviation — the ~30s log granularity cannot support a faithful replay. The assumption line ("held to settlement, 100 contracts, no market impact") is stated on every declined entry. Signals without a price, or windows without a `yes`/`no` result, still list with `counterfactual_pnl_cents: None`; malformed JSON lines are skipped. This is how thresholds get honest feedback — declines that keep "winning" counterfactually push thresholds down at review; declines that keep "losing" validate them.

### Outputs — the daily aggregate note
13. `04-trade-history/postmortems/{YYYY-MM-DD}-{family}.md` — **one note per family per UTC day**, dated by `closes_at` (falling back to run time). Rationale: Obsidian degrades around ~10K files per folder; per-window notes at 96/day reach that in weeks. Each window appends a `## {event_id} — {result|pending}` section: narrative, mismatch block (rule 4), vol flag (rule 10), drift line (rule 11), a machine-readable meta line (strike/expiration/realized vol/trade & declined counts/P&L), then **one markdown table, one row per order** (a real trade or a declined candidate) — or the italic line "_watched, nothing traded — that is data_" when there were none. Frontmatter accumulates `windows`, `trades`, `realized_pnl_cents`, `counterfactual_pnl_cents`, and `settled_events` across the day.

13a. **The orders table** (`_orders_table`, one per window section): columns `Type | Order | Skill | Side | Entry¢ | Contracts | Result | P&L¢ | Slippage¢ | Model | Flags`. `Type` is the only thing distinguishing a real fill from a hypothetical — `trade` rows carry real P&L and an Obsidian wikilink (`[[coid]]`) to the actual trade note; `declined` rows carry the held-to-settlement counterfactual at the normalized `CF_CONTRACTS` (100, shown as `100 (cf)` in the Contracts column) and no `Skill` (a declined candidate was never matched to one). `Model` is ✓/✗/— for both row types — declined candidates get the same model-was-right check as real trades, just against the hypothetical side. Missing values render as `—`, never a blank cell or a fabricated number.
14. **Narrative:** a deterministic 2–4 sentence block per section (`_narrative` — templated, **no LLM in the trading loop, ever**), heavy on model-vs-market and vol-regime commentary ("realized vol came in far above what the model priced — vol input quality is this skill's #1 failure mode").

### Skill stats — sole writer, batched
15. `run()` returns `outcomes_by_skill` (rows: `pnl_cents`, `entry_price_cents`, `excluded`, `event_id` — only trades with a non-`None` P&L and a `skill`). `update_skill_stats()` applies a **batch** to skill-note frontmatter via `vault.update_frontmatter(path, updates, caller="analyst")` (the scope table admits only these fields): `{prefix}sample_size += settled trades`, `{prefix}win_rate = (old_wr × old_n + wins) / new_n` (4 dp), where `prefix` is `demo_` when `env == "demo"` and empty (the legacy prod pair) otherwise. A "win" = P&L > 0 net of fees. `excluded` rows (constituent drift) don't count; a missing skill note skips that skill without failing. The analyst flushes the batch every `ROLLUP_WINDOWS` = 4 settled windows, alongside the Discord rollup — never per window (write amplification at 96 windows/day). This module remains the **sole writer** of `win_rate`/`sample_size`.
16. **Threshold review flag:** when a skill's updated (env-appropriate) `sample_size ≥ REVIEW_MIN_SAMPLE` AND `|new win_rate − breakeven| ≥ REVIEW_DELTA`, where breakeven = mean `entry_price_cents` of *this batch's* rows for that skill ÷ 100, `update_skill_stats` returns a `⚠ THRESHOLD REVIEW` flag string (the analyst appends it to the rollup message). **Humans change thresholds; this skill only surfaces evidence** — it never edits conditions, thresholds, or `status`.

### Discord throttling (analyst-side, but part of this skill's contract)
17. Quiet windows stay quiet: one `ROLLUP` message per `ROLLUP_WINDOWS` = 4 settled windows (hourly at the 15-min cadence — totals, per-window one-liners, threshold flags). A window that actually traded gets its own `POSTMORTEM` card immediately (P&L, model hits, vol ratio, drift). Settlement mismatch notifies `critical` and raises; the give-up path notifies `warning`.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `REVIEW_MIN_SAMPLE` | 20 | CONFIRMED 2026-07-17 — threshold-review flag floor |
| `REVIEW_DELTA` | 0.10 | CONFIRMED 2026-07-17 — win-rate divergence trigger |
| `VOL_RATIO_BAND` | (0.5, 2.0) | PROPOSED 2026-07-22 — outside → vol-was-wrong flag |
| `CF_CONTRACTS` | 100 | PROPOSED 2026-07-22 — carried from v1, normalized analysis-only size, pending owner confirmation |
| `SECONDS_PER_YEAR` | 31_536_000 | annualization base — must match the live vol estimator |
| `ROLLUP_WINDOWS` (analyst) | 4 | PROPOSED 2026-07-22 — Discord rollup + stats-flush batch size |
| `SETTLE_POLL_INTERVAL_S` (analyst) | 5.0 | PROPOSED 2026-07-22 — REST check cadence per pending window |
| `SETTLE_GIVE_UP_S` (analyst) | 900.0 | PROPOSED 2026-07-22 — unfinalized past this = incident: pending report + warning |

## Edge cases
- **Window note missing/corrupt:** postmortem proceeds with trade notes as ground truth, logs a RECORD GAP warning (a record-keeping bug is itself a finding); counterfactuals, realized vol, drift detection, and the settlement cross-check are all skipped (no signal log, no strike).
- **Pending is final in the daily note:** a pending window's section is written and its `event_id` enters `settled_events`, so a later `run` short-circuits — there is no fill-in-later re-run. The analyst's 900s poll *is* the retry mechanism; a give-up is an incident a human follows up on.
- **Voided market:** `settlement_status: voided`; counterfactuals skipped (no true outcome); P&L reconstruction skipped too (rule 8 fires on `settled`/`mismatch` only), so a voided trade without recorded P&L stays open and out of stats — the v1 full-loss reconstruction caveat is fixed.
- **UTC midnight:** the note path dates from `closes_at`, not run time — a window closing 23:59 UTC audited after midnight still lands in the correct day's note (v1's second-note caveat is fixed).
- **No Discord bot wired:** a mismatch still writes the note and raises; only the notify is skipped.
- **Zero trades, zero candidates:** section still written — "we watched, nothing fired" is data.
- **Mismatch mid-batch:** `SettlementMismatch` propagates through the analyst; the mismatched window's outcomes never enter the stats batch.

## Dependencies
vault (all reads on the batch path + all writes; `caller="analyst"` scope), kalshi-client's `est_fee_cents` (module import, counterfactual entry fees — the constructor's `kalshi_client` handle is kept for interface stability but `run()` places no API calls), window-monitor's active-window note format (frontmatter `strike` + `- SIGNAL {json}` + `- LOG {json}` lines) as the input contract, discord-bot (optional; critical notify on mismatch). Orchestrated by the analyst agent (`kalshi_bots/agents/analyst.py`): settlement polling, paper-broker settlement, `settled_direction` stamping, rollup/stats batching. Never on the live path.

## Testing requirements
- `window_realized_vol`: hand-computed alternating-return fixture matches the sqrt(dt)/annualized formula exactly; too few samples → `None`.
- Audit: settled win books derived P&L, closes the trade note (`exit_reason: held_to_settlement`), counts a model hit; entry-condition `False` → violation + `⚠ ENTRY VIOLATION` flag cell; model miss → `✗` in the row's Model column.
- Cross-check: expiration vs strike implying the opposite of Kalshi's result → `SettlementMismatch` raised, note written first.
- Pending: `market_result=None` → `settlement_status: pending`, open trades untouched, empty outcomes.
- Crypto counterfactuals: violent spot path vs recorded sigma → `vol_ratio > 2.0` + VOL-WAS-WRONG flag; one degraded `healthy` sample → `constituent_drift` and `excluded` outcome rows.
- Orders table: a real trade renders as a `trade` row with a working `[[coid]]` wikilink and real P&L; a signal-log line with no matching trade renders as a `declined` row with the known 100-contract held-to-settlement P&L (real fee formula) and its own model-hit mark; zero orders still writes the "watched, nothing traded" line, no empty table.
- Daily aggregate: two windows share one note (accumulated `windows`/`settled_events`, both `##` sections present); re-running a settled window doesn't double-count.
- Batched stats: demo trades update `demo_win_rate`/`demo_sample_size` with prod fields untouched; accumulation across successive batches is incremental-mean-correct; excluded rows don't count; ≥ 20 samples with ≥ 0.10 divergence → THRESHOLD REVIEW flag.
- Analyst side (`tests/test_analyst.py`): polls until finalized with the per-window throttle; paper broker settles from the market result; `settled_direction` written to the window note; give-up after 900s → pending report + warning; quiet windows batch into exactly one ROLLUP; a traded window notifies immediately; stats flush per batch, not per window.

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
    # crypto counterfactual dimensions — per-window coin-flippy, aggregated
    # by the analyst
    model_direction_hits: int = 0    # trades whose side matched settlement
    vol_ratio: float | None = None   # window realized vol / mean sigma_used
    constituent_drift: bool = False  # feed degraded in-window -> excluded

# Daily note frontmatter: {family, env, date, windows, trades,
#   realized_pnl_cents, counterfactual_pnl_cents, settled_events: list[str]}
# outcomes_by_skill row: {pnl_cents, entry_price_cents, excluded, event_id}
```
