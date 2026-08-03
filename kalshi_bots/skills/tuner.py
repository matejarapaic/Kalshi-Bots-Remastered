"""tuner skill. Spec: skills/tuner/SKILL.md.

Streak-driven live adjustment of risk parameters through risk-management's
override layer. Loss streaks tighten (smaller sizing, less exposure, more
required edge); wins and no-trade streaks relax — and, owner-directed
2026-07-30, keep relaxing past the human-approved baseline for as long as
the streak continues, uncapped except by each parameter's own domain. All
movement is corridor-clamped by risk_management.set_override; this module
never invents a risk number of its own, it only steps existing ones
(CONTRACTS.md rule 5).
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshi_bots.skills import risk_management as rm
from kalshi_bots.types import ParamAdjustment, PostmortemReport

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuner trigger parameters. PROPOSED 2026-07-28 (initial live-tuning defaults,
# awaiting owner sign-off). These govern *when* the tuner moves the risk
# parameters — the corridors governing *how far* live in
# risk_management.TUNABLE_BOUNDS next to the baselines they bound.
# ---------------------------------------------------------------------------
LOSS_STREAK_TRIGGER = 3      # consecutive settled losing windows -> tighten
NO_TRADE_STREAK_TRIGGER = 3  # consecutive settled tradeless windows (~45m) -> relax
TIGHTEN_STEP = 0.85          # multiplicative step for lower-is-tighter params
EDGE_STEP_CENTS = 1          # additive step for MIN_EDGE_CENTS (raise-is-tighter)

# Sigma-floor policy (CONFIRMED 2026-08-02, owner-directed): SIGMA_PLAUSIBLE_MIN
# is deliberately NOT in POLICY_PARAMS — the streak relax/tighten loops never
# touch it. It moves through exactly one mechanism: after
# SIGMA_LOSS_STREAK_TRIGGER consecutive losing windows, the floor is raised
# to the session's average realized vol (mean over the last
# SIGMA_SESSION_WINDOWS settled windows that had a vol reading) if that
# average is above the current floor. Raise-only: the tuner never lowers the
# sigma floor — wins and no-trade streaks leave it where it is, and only an
# owner edit/override-clear brings it back down. Corridor-clamped like
# everything else (tighten side of TUNABLE_BOUNDS caps it at 2x baseline).
SIGMA_LOSS_STREAK_TRIGGER = 2   # consecutive losing windows -> sigma tighten
SIGMA_SESSION_WINDOWS = 20      # session-average lookback, bounded deque (~5h)

# The streak policy moves this set — the highest-leverage sizing/entry/exposure
# dials. Relaxing walks the same set back. (None = system-wide, "per-skill" =
# one override per skill in the risk table.)
POLICY_PARAMS: tuple[tuple[str, bool], ...] = (
    ("SKILL_RISK_MULTIPLIER", True),
    ("PER_TRADE_CAP_PCT", True),
    ("MAX_CONTRACTS_PER_WINDOW", False),
    ("TOTAL_EXPOSURE_CAP_PCT", False),
    ("MAX_OPEN_POSITIONS", False),
    ("STOP_LOSS_PCT", False),
    ("MIN_EDGE_CENTS", False),
)


@dataclass
class TunerState:
    """Window-level streak counters (PostmortemReport carries no per-skill
    split; with one confirmed skill trading, window == skill in practice).
    `recent_sigmas` is the sigma-floor policy's session memory: realized vol
    of the last SIGMA_SESSION_WINDOWS settled windows (bounded deque — 24/7
    hygiene)."""
    loss_streak: int = 0
    no_trade_streak: int = 0
    windows_seen: int = 0
    recent_sigmas: deque = field(
        default_factory=lambda: deque(maxlen=SIGMA_SESSION_WINDOWS))


def record_window(state: TunerState, report: PostmortemReport) -> str:
    """Update streaks from one report. Returns the window's classification:
    'win' | 'loss' | 'no_trade' | 'skipped'. Only non-settled windows are
    skipped. `constituent_drift` deliberately does NOT skip a window here
    (fixed 2026-07-31, found live): the flag means our own composite feed
    blipped mid-window — the right reason for postmortem to exclude the
    window from win_rate/sample_size learning, but trades_audited and
    realized_pnl_cents come from real Kalshi fills and settlements, which
    our feed's health can't retroactively invalidate. Reusing the flag here
    zeroed the streaks permanently in live operation (~every window has at
    least one 5-venue health blip), so the tuner never fired once."""
    if report.settlement_status != "settled":
        return "skipped"
    state.windows_seen += 1
    if report.realized_vol is not None:
        state.recent_sigmas.append(report.realized_vol)
    if report.trades_audited == 0:
        state.no_trade_streak += 1
        return "no_trade"
    state.no_trade_streak = 0
    if report.realized_pnl_cents < 0:
        state.loss_streak += 1
        return "loss"
    state.loss_streak = 0
    return "win"


def _raises_to_tighten(name: str) -> bool:
    return rm.TUNABLE_BOUNDS[name][1] > 1.0


def _tighten_value(name: str, skill: str | None):
    """One tighten step from the current effective value, clamped to the
    corridor. Returns (cur, new) or None if the param can't move further."""
    base = rm.baseline(name, skill)
    if base is None:
        return None
    cur = rm.current(name, skill)
    lo_m, hi_m = rm.TUNABLE_BOUNDS[name]
    if _raises_to_tighten(name):
        hi = hi_m * base
        new = min(cur + EDGE_STEP_CENTS, int(hi) if isinstance(base, int) else hi)
        return None if new <= cur else (cur, new)
    if isinstance(base, tuple):
        lo = tuple(lo_m * b for b in base)
        new = tuple(max(l, v * TIGHTEN_STEP) for l, v in zip(lo, cur))
        return None if new == tuple(cur) else (cur, new)
    lo = lo_m * base
    if isinstance(base, int):
        lo = max(1, math.floor(lo))
        new = max(lo, int(round(cur * TIGHTEN_STEP)))
        if new >= cur:  # integer rounding stall (e.g. 2 * 0.85 rounds back to 2)
            new = max(lo, cur - 1)
    else:
        new = max(lo, cur * TIGHTEN_STEP)
    return None if new >= cur else (cur, new)


def _relax_value(name: str, skill: str | None):
    """One relax step in the loosening direction — unbounded past baseline
    (owner-directed 2026-07-30): relaxing no longer stops once it reaches the
    human-approved baseline, it keeps stepping past it for as long as
    winning/no-trade streaks continue. risk_management.set_override's
    corridor is the only remaining floor/ceiling (0 for quantities that can't
    go negative, uncapped otherwise). Returns (cur, new) or None if the
    param's value can't move (rounding stall)."""
    base = rm.baseline(name, skill)
    cur = rm.current(name, skill)
    if _raises_to_tighten(name):
        new = cur - EDGE_STEP_CENTS
    elif isinstance(base, tuple):
        new = tuple(v / TIGHTEN_STEP for v in cur)
    elif isinstance(base, int):
        new = max(cur + 1, int(round(cur / TIGHTEN_STEP)))
    else:
        new = cur / TIGHTEN_STEP
    return None if new == cur else (cur, new)


def _policy_targets():
    for name, per_skill in POLICY_PARAMS:
        if per_skill:
            for skill in getattr(rm, name):
                yield name, skill
        else:
            yield name, None


def tighten_all(reason: str) -> list[ParamAdjustment]:
    now = datetime.now(timezone.utc)
    out: list[ParamAdjustment] = []
    for name, skill in _policy_targets():
        step = _tighten_value(name, skill)
        if step is None:
            continue
        try:
            old, new = rm.set_override(name, step[1], skill=skill,
                                       reason=reason, caller="tuner")
        except rm.RiskError as e:
            log.warning("tuner tighten rejected for %s[%s]: %s", name, skill, e)
            continue
        out.append(ParamAdjustment(param=name, skill=skill, old_value=old,
                                   new_value=new, reason=reason, ts=now))
    return out


def relax_all(reason: str) -> list[ParamAdjustment]:
    now = datetime.now(timezone.utc)
    out: list[ParamAdjustment] = []
    for name, skill in _policy_targets():
        step = _relax_value(name, skill)
        if step is None:
            continue
        cur, new = step
        try:
            _, new = rm.set_override(name, new, skill=skill,
                                     reason=reason, caller="tuner")
        except rm.RiskError as e:
            log.warning("tuner relax rejected for %s[%s]: %s", name, skill, e)
            continue
        out.append(ParamAdjustment(param=name, skill=skill, old_value=cur,
                                   new_value=new, reason=reason, ts=now))
    return out


def tighten_sigma_floor(state: TunerState, reason: str) -> list[ParamAdjustment]:
    """Raise SIGMA_PLAUSIBLE_MIN to the session's average realized vol
    (raise-only; corridor-clamped by set_override). No-op when the session
    average is at/below the current floor (the gate is already tighter than
    the session's typical regime) or when no vol readings exist yet. This is
    the ONLY tuner path that moves the sigma floor — it is not a policy
    param, and nothing here ever lowers it."""
    if not state.recent_sigmas:
        return []
    session_avg = sum(state.recent_sigmas) / len(state.recent_sigmas)
    cur = rm.current("SIGMA_PLAUSIBLE_MIN")
    base = rm.baseline("SIGMA_PLAUSIBLE_MIN")
    ceiling = rm.TUNABLE_BOUNDS["SIGMA_PLAUSIBLE_MIN"][1] * base
    new = min(session_avg, ceiling)
    if new <= cur:
        return []
    try:
        old, new = rm.set_override("SIGMA_PLAUSIBLE_MIN", new,
                                   reason=reason, caller="tuner")
    except rm.RiskError as e:
        log.warning("tuner sigma-floor tighten rejected: %s", e)
        return []
    return [ParamAdjustment(param="SIGMA_PLAUSIBLE_MIN", skill=None,
                            old_value=old, new_value=new, reason=reason,
                            ts=datetime.now(timezone.utc))]


def apply_feedback(state: TunerState,
                   report: PostmortemReport) -> list[ParamAdjustment]:
    """Record one settled window and apply whatever the streak policy calls
    for. Tighten fires at every LOSS_STREAK_TRIGGER-multiple of the loss
    streak; a win relaxes one step; a no-trade streak relaxes one step at
    every NO_TRADE_STREAK_TRIGGER-multiple (it can only undo prior
    tightening — the baseline is the ceiling, so a no-trade streak with no
    active overrides is a no-op). Separately, once the loss streak reaches
    SIGMA_LOSS_STREAK_TRIGGER, every further loss also re-checks the sigma
    floor against the session-average vol (raise-only; usually a no-op after
    the first raise)."""
    outcome = record_window(state, report)
    out: list[ParamAdjustment] = []
    if outcome == "loss" and state.loss_streak >= SIGMA_LOSS_STREAK_TRIGGER:
        out.extend(tighten_sigma_floor(
            state, f"{state.loss_streak} consecutive losing windows — "
                   f"sigma floor raised to session average vol"))
    if outcome == "loss" and state.loss_streak >= LOSS_STREAK_TRIGGER \
            and state.loss_streak % LOSS_STREAK_TRIGGER == 0:
        out.extend(tighten_all(
            f"{state.loss_streak} consecutive losing windows"))
        return out
    if outcome == "win":
        return relax_all("winning window")
    if outcome == "no_trade" and state.no_trade_streak >= NO_TRADE_STREAK_TRIGGER \
            and state.no_trade_streak % NO_TRADE_STREAK_TRIGGER == 0:
        return relax_all(
            f"{state.no_trade_streak} consecutive windows without a trade")
    return out
