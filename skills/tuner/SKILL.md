# tuner

**Trigger:** the analyst's settlement poll produced settled-window postmortem reports; performance streaks (consecutive losses, consecutive tradeless windows) may warrant a live risk-parameter adjustment.

## What this is for

Streak-driven live adjustment of risk parameters through risk-management's override layer (see that spec's "Live overrides" section), so the system responds to sustained bad performance or sustained inactivity *within a running session* instead of waiting for a human redeploy. The tuner never invents a risk number — it steps existing parameters inside owner-approved corridors. Tighten is still one-directional and hard-bounded: it can only make the system more conservative than the human-confirmed baseline, up to the corridor's tighten-side limit.

Relax is **not** bounded at baseline. Authorized 2026-07-28 by owner decision (fully autonomous, no per-change approval, all `risk_management.py` parameters in mechanism scope, simple streak-count triggers); the corridor's ceiling-at-baseline bound on the relax side was the design's original safety envelope, and removing it required an explicit, direct owner instruction — given 2026-07-30. As of that change, a winning window or a no-trade streak at its trigger multiple relaxes every policy param one step *away* from tighten, with no floor at baseline: it keeps going, indefinitely, for as long as the streak continues. The only remaining limit is each parameter's own domain (e.g. `MIN_EDGE_CENTS` floors at 0 — can't demand negative edge; sizing/exposure params like `SKILL_RISK_MULTIPLIER`, `PER_TRADE_CAP_PCT`, `TOTAL_EXPOSURE_CAP_PCT`, `MAX_OPEN_POSITIONS`, `STOP_LOSS_PCT` have no ceiling at all). Concretely: enough consecutive winning or tradeless windows can now size positions larger than originally authorized, hold more open positions, tolerate bigger per-position losses before the stop-loss backstop fires, and require less edge to enter than the owner signed off on 2026-07-22. This is a real increase in autonomous risk-taking capacity, not a cosmetic change — read `risk_management.py`'s override-layer comment before changing any of this further.

## Interface

```python
# skill module (kalshi_bots/skills/tuner.py) — pure policy over rm overrides
record_window(state: TunerState, report: PostmortemReport) -> str   # 'win'|'loss'|'no_trade'|'skipped'
tighten_all(reason: str) -> list[ParamAdjustment]   # one corridor-clamped step down on every policy param
relax_all(reason: str) -> list[ParamAdjustment]     # one step away from tighten, unbounded past baseline
tighten_sigma_floor(state: TunerState, reason: str) -> list[ParamAdjustment]  # raise SIGMA_PLAUSIBLE_MIN to session-avg vol (raise-only)
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
| `NO_TRADE_STREAK_TRIGGER` | 3 | consecutive settled tradeless windows (~45m) → one relax step (again at 6, 9, …) |
| `TIGHTEN_STEP` | 0.85 | multiplicative step for lower-is-tighter params; relax divides by it |
| `EDGE_STEP_CENTS` | 1 | additive step for raise-is-tighter params (`MIN_EDGE_CENTS`) |

### CONFIRMED 2026-08-02 (sigma-floor policy, owner-directed)
| Name | Value | Meaning |
|---|---|---|
| `SIGMA_LOSS_STREAK_TRIGGER` | 2 | consecutive losing windows → raise `SIGMA_PLAUSIBLE_MIN` to the session-average realized vol (re-checked on every further loss while the streak holds; usually a no-op after the first raise) |
| `SIGMA_SESSION_WINDOWS` | 20 | session-average lookback: bounded deque of the last N settled windows' realized vol (~5h at 15-min cadence) |

The corridors bounding *how far* any parameter can move live in `risk_management.TUNABLE_BOUNDS`, next to the baselines they bound — not here.

## Behavior

1. **Window classification** (`record_window`): only `settlement_status == "settled"` reports count. `constituent_drift` does **not** skip a window (fixed 2026-07-31, found live): the flag marks a mid-window blip in *our own* composite feed — the right exclusion for postmortem's win_rate/sample_size learning, but `trades_audited`/`realized_pnl_cents` come from real Kalshi fills and settlements, which our feed's health can't invalidate. The old shared skip zeroed the streaks permanently in live operation (virtually every 15-min window has at least one 5-venue health blip), so the tuner never fired once despite settled trades. `trades_audited == 0` → extends the no-trade streak; a traded window resets it. Among traded windows, `realized_pnl_cents < 0` extends the loss streak; a non-losing traded window resets it. No-trade windows leave the loss streak untouched (no new information about trade quality). Streaks are window-level — `PostmortemReport` carries no per-skill split, and with one confirmed skill trading, window ≈ skill in practice.
2. **Tighten** fires when the loss streak reaches each `LOSS_STREAK_TRIGGER` multiple. One corridor-clamped step on every policy param (`POLICY_PARAMS`): `SKILL_RISK_MULTIPLIER` (per skill), `PER_TRADE_CAP_PCT` (per skill), `MAX_CONTRACTS_PER_WINDOW`, `TOTAL_EXPOSURE_CAP_PCT`, `MAX_OPEN_POSITIONS`, `STOP_LOSS_PCT` step down ×`TIGHTEN_STEP`; `MIN_EDGE_CENTS` steps up +`EDGE_STEP_CENTS`. A param already at its corridor edge is skipped (no error, no adjustment reported). Integer params round; a rounding stall (e.g. 2×0.85 rounding back to 2) forces at least a −1 step, still floor-clamped.
2b. **Sigma floor** (owner-directed 2026-08-02): `SIGMA_PLAUSIBLE_MIN` is deliberately **not** a policy param — the streak tighten/relax loops never touch it, and no-trade streaks can never lower it. It moves through exactly one mechanism, `tighten_sigma_floor`: once the loss streak reaches `SIGMA_LOSS_STREAK_TRIGGER` (2), the floor is raised to the session's average realized vol — the mean over `TunerState.recent_sigmas`, a bounded deque of the last `SIGMA_SESSION_WINDOWS` settled windows' `realized_vol` (populated in `record_window` from every settled report, traded or not). Raise-only and corridor-clamped (tighten side of `TUNABLE_BOUNDS` caps it at 2× baseline): if the session average is at or below the current floor, or no vol readings exist yet, it is a no-op. The tuner never lowers the sigma floor — wins and no-trade streaks leave it in place; only an owner edit or override-clear brings it back down. Rationale: after consecutive losses, stop trusting the model in the session's prevailing vol regime until vol rises above the session's own average.
3. **Relax** fires on every winning window and at each `NO_TRADE_STREAK_TRIGGER` multiple of the no-trade streak. One step away from tighten on every policy param, regardless of whether an override is currently active (divide by `TIGHTEN_STEP` / subtract `EDGE_STEP_CENTS`) — this now continues *past* baseline rather than clamping to it, so a long enough winning or no-trade streak pushes a param looser than the owner-confirmed baseline. There is no "reached baseline, clear the override" step anymore; the override simply keeps moving every time relax fires.
4. Every applied adjustment goes through `risk_management.set_override(..., caller="tuner")`, which re-validates against the corridor and appends to the module-level `override_log` — the tuner inherits that audit trail rather than owning enforcement. A rejected override (corridor violation, e.g. a baseline edited since state was persisted) is logged and skipped, never force-applied.
5. **Announcements:** each adjustment is `discord.notify`-ed (tighten → `warning`, relax → `info`) and emitted as a `tuner-adjustment` orchestrator event for the dashboard. Not approval-gated — this is fire-and-forget, matching the autonomous execution mode.

## Persistence

`03-market-context/tuner-state.md` (caller `"tuner"`, scope: `03-market-context` only). Frontmatter = machine state (streak counters, `recent_sigmas` — the sigma-floor policy's session lookback, `active_overrides` as `"NAME"`/`"NAME|skill"` → value, `env`, `updated`); body = human-readable changelog table (bounded, last 50 adjustments). Written after every tick that processed reports. On startup `Tuner.reload()` restores the counters and re-applies each stored override through `set_override` — re-validated, so a stale override outside the current corridor is dropped with a warning.

## Edge cases

- **Nothing movable on tighten:** every policy param already at its corridor floor → `tighten_all` returns `[]`; the streak keeps counting and the daily-loss halt / stop-loss remain the harder backstops.
- **Relax with no overrides:** still fires — a win or no-trade-streak trigger relaxes every policy param one step past its current value (baseline, if nothing was overridden yet) and keeps going with each subsequent trigger. No ceiling other than each parameter's own domain.
- **Voided / mismatch / pending windows:** skipped — they neither extend nor reset streaks.
- **Baseline changed under a persisted override:** reload re-validation drops the override (warning) rather than applying an out-of-corridor value.
- **`SKILL_RISK_MULTIPLIER` is `None` (flat-sized skill):** no numeric baseline → that param is skipped for the skill.

## Dependencies

risk-management (override layer: `current`/`set_override`/`clear_override`/`has_override`/`baseline`/`TUNABLE_BOUNDS`), vault (state persistence), discord-bot (`notify`), postmortem (consumes its `PostmortemReport`s via the analyst's `poll_pending` return). Called by: orchestrator only, once per tick after the settlement poll.

## Testing requirements

(`tests/test_tuner.py`)
- `TestStreakCounting`: win/loss/no-trade/skipped classification; no-trade doesn't reset loss streak; a traded window resets no-trade streak; non-settled reports skipped; drift-flagged windows still count toward streaks.
- `TestTighten`: fires exactly at trigger multiples; every policy param steps once and is corridor-clamped; params at the floor are skipped; integer rounding stall forces −1; `MIN_EDGE_CENTS` rises.
- `TestSigmaFloor`: fires at 2 consecutive losses, not 1; raises to session-average vol; raise-only (average at/below current floor → no-op); corridor-capped at 2× baseline; relax (win / no-trade streak) never moves it; windows without a vol reading don't poison the average; state round-trips through persist/reload.
- `TestRelax`: a win relaxes one step; relaxing continues past baseline rather than clamping to it; relaxing fires and moves the param even with no active override yet; no-trade streak relaxes at trigger multiples only.
- `TestAgent`: `on_reports` persists state and re-applies on `reload()` (restart round-trip); a corridor-rejected stored override is dropped; adjustments notify Discord with the right level.
