# tuner

**Trigger:** the analyst's settlement poll produced settled-window postmortem reports; performance streaks (consecutive losses, consecutive tradeless windows) may warrant a live risk-parameter adjustment.

## What this is for

Streak-driven live adjustment of risk parameters through risk-management's override layer (see that spec's "Live overrides" section), so the system responds to sustained bad performance or sustained inactivity *within a running session* instead of waiting for a human redeploy. The tuner never invents a risk number — it steps existing parameters inside owner-approved corridors, and its authority is one-directional: it can make the system **more conservative than the human-confirmed baseline, never less**. A no-trade streak can undo the tuner's own earlier tightening; it can never push a parameter looser than the owner signed off on.

Authorized 2026-07-28 by owner decision: fully autonomous (no per-change approval), all `risk_management.py` parameters in mechanism scope, simple streak-count triggers. The corridor ceiling-at-baseline bound is the design's standing safety envelope — removing it requires an explicit, direct owner instruction, same rule as every other prod/live gate.

## Interface

```python
# skill module (kalshi_bots/skills/tuner.py) — pure policy over rm overrides
record_window(state: TunerState, report: PostmortemReport) -> str   # 'win'|'loss'|'no_trade'|'skipped'
tighten_all(reason: str) -> list[ParamAdjustment]   # one corridor-clamped step down on every policy param
relax_all(reason: str) -> list[ParamAdjustment]     # one step back toward baseline; clears overrides that reach it
apply_feedback(state: TunerState, report: PostmortemReport) -> list[ParamAdjustment]  # record + decide + apply

# agent (kalshi_bots/agents/tuner.py) — thin wrapper, orchestrator-invoked
Tuner(vault, discord=None, env="demo")
Tuner.reload() -> None                                        # startup: restore streaks, re-apply persisted overrides
Tuner.on_reports(reports: list[PostmortemReport]) -> list[ParamAdjustment]  # per tick, after analyst.poll_pending
```

## THE PARAMETER TABLE

### PROPOSED 2026-07-28 (initial live-tuning defaults, awaiting owner sign-off)
| Name | Value | Meaning |
|---|---|---|
| `LOSS_STREAK_TRIGGER` | 3 | consecutive settled losing windows → one tighten step (and again at every further multiple: 6, 9, …) |
| `NO_TRADE_STREAK_TRIGGER` | 8 | consecutive settled tradeless windows (~2h) → one relax step (again at 16, 24, …) |
| `TIGHTEN_STEP` | 0.85 | multiplicative step for lower-is-tighter params; relax divides by it |
| `EDGE_STEP_CENTS` | 1 | additive step for raise-is-tighter params (`MIN_EDGE_CENTS`) |

The corridors bounding *how far* any parameter can move live in `risk_management.TUNABLE_BOUNDS`, next to the baselines they bound — not here.

## Behavior

1. **Window classification** (`record_window`): only `settlement_status == "settled"` reports count; `constituent_drift` windows are skipped entirely (same exclusion postmortem applies to win-rate learning). `trades_audited == 0` → extends the no-trade streak; a traded window resets it. Among traded windows, `realized_pnl_cents < 0` extends the loss streak; a non-losing traded window resets it. No-trade windows leave the loss streak untouched (no new information about trade quality). Streaks are window-level — `PostmortemReport` carries no per-skill split, and with one confirmed skill trading, window ≈ skill in practice.
2. **Tighten** fires when the loss streak reaches each `LOSS_STREAK_TRIGGER` multiple. One corridor-clamped step on every policy param (`POLICY_PARAMS`): `SKILL_RISK_MULTIPLIER` (per skill), `PER_TRADE_CAP_PCT` (per skill), `MAX_CONTRACTS_PER_WINDOW`, `TOTAL_EXPOSURE_CAP_PCT`, `MAX_OPEN_POSITIONS`, `STOP_LOSS_PCT` step down ×`TIGHTEN_STEP`; `MIN_EDGE_CENTS` steps up +`EDGE_STEP_CENTS`. A param already at its corridor edge is skipped (no error, no adjustment reported). Integer params round; a rounding stall (e.g. 2×0.85 rounding back to 2) forces at least a −1 step, still floor-clamped.
3. **Relax** fires on every winning window and at each `NO_TRADE_STREAK_TRIGGER` multiple of the no-trade streak. One step back toward baseline per active override (divide by `TIGHTEN_STEP` / subtract `EDGE_STEP_CENTS`); an override that reaches its baseline (within float tolerance) is **cleared**, so the param fully re-tracks the module constant. With no active overrides, relax is a no-op — the baseline is the ceiling.
4. Every applied adjustment goes through `risk_management.set_override(..., caller="tuner")`, which re-validates against the corridor and appends to the module-level `override_log` — the tuner inherits that audit trail rather than owning enforcement. A rejected override (corridor violation, e.g. a baseline edited since state was persisted) is logged and skipped, never force-applied.
5. **Announcements:** each adjustment is `discord.notify`-ed (tighten → `warning`, relax → `info`) and emitted as a `tuner-adjustment` orchestrator event for the dashboard. Not approval-gated — this is fire-and-forget, matching the autonomous execution mode.

## Persistence

`03-market-context/tuner-state.md` (caller `"tuner"`, scope: `03-market-context` only). Frontmatter = machine state (streak counters, `active_overrides` as `"NAME"`/`"NAME|skill"` → value, `env`, `updated`); body = human-readable changelog table (bounded, last 50 adjustments). Written after every tick that processed reports. On startup `Tuner.reload()` restores the counters and re-applies each stored override through `set_override` — re-validated, so a stale override outside the current corridor is dropped with a warning.

## Edge cases

- **Nothing movable on tighten:** every policy param already at its corridor floor → `tighten_all` returns `[]`; the streak keeps counting and the daily-loss halt / stop-loss remain the harder backstops.
- **Relax with no overrides:** no-op, `[]`. The tuner never loosens past baseline no matter how long the drought.
- **Voided / mismatch / pending windows:** skipped — they neither extend nor reset streaks.
- **Baseline changed under a persisted override:** reload re-validation drops the override (warning) rather than applying an out-of-corridor value.
- **`SKILL_RISK_MULTIPLIER` is `None` (flat-sized skill):** no numeric baseline → that param is skipped for the skill.

## Dependencies

risk-management (override layer: `current`/`set_override`/`clear_override`/`has_override`/`baseline`/`TUNABLE_BOUNDS`), vault (state persistence), discord-bot (`notify`), postmortem (consumes its `PostmortemReport`s via the analyst's `poll_pending` return). Called by: orchestrator only, once per tick after the settlement poll.

## Testing requirements

(`tests/test_tuner.py`)
- `TestStreakCounting`: win/loss/no-trade/skipped classification; no-trade doesn't reset loss streak; a traded window resets no-trade streak; drift and non-settled reports skipped.
- `TestTighten`: fires exactly at trigger multiples; every policy param steps once and is corridor-clamped; params at the floor are skipped; integer rounding stall forces −1; `MIN_EDGE_CENTS` rises.
- `TestRelax`: a win relaxes one step; overrides reaching baseline are cleared (`has_override` False); relax never crosses baseline; no-op with no overrides; no-trade streak relaxes at trigger multiples only.
- `TestAgent`: `on_reports` persists state and re-applies on `reload()` (restart round-trip); a corridor-rejected stored override is dropped; adjustments notify Discord with the right level.
