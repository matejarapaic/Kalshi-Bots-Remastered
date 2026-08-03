"""tuner agent. Prompt: vault 01-agents/tuner/system-prompt.md.

Consumes settled-window postmortem reports and adjusts risk parameters live
through risk-management's override layer: tighten on loss streaks; wins and
no-trade streaks relax, and — owner-directed 2026-07-30 — keep relaxing past
the human-approved baseline for as long as the streak continues. Never
places orders, never sizes, never writes skill stats — the streak policy
itself lives in skills/tuner.py; this wrapper only persists state and
announces changes.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone

from kalshi_bots.skills import risk_management as rm
from kalshi_bots.skills import tuner as tuner_skill
from kalshi_bots.skills.tuner import TunerState
from kalshi_bots.types import ParamAdjustment, PostmortemReport

log = logging.getLogger(__name__)

STATE_PATH = "03-market-context/tuner-state.md"
CHANGELOG_MAX = 50  # rows kept in the state note body (24/7 hygiene)


def _is_tighten(a: ParamAdjustment) -> bool:
    old = a.old_value[0] if isinstance(a.old_value, tuple) else a.old_value
    new = a.new_value[0] if isinstance(a.new_value, tuple) else a.new_value
    raises = rm.TUNABLE_BOUNDS[a.param][1] > 1.0
    return new > old if raises else new < old


class Tuner:
    def __init__(self, vault, discord=None, env: str = "demo"):
        self.vault = vault
        self.discord = discord
        self.env = env
        self.state = TunerState()
        self.changelog: deque[ParamAdjustment] = deque(maxlen=CHANGELOG_MAX)

    # --- restart recovery ---

    def reload(self) -> None:
        """Restore streak counters and re-apply persisted overrides. An
        override the corridor no longer accepts (baseline edited since) is
        dropped with a warning, never force-applied."""
        try:
            note = self.vault.read_note(STATE_PATH)
        except Exception:
            return  # fresh state
        fm = note.frontmatter
        self.state.loss_streak = int(fm.get("loss_streak") or 0)
        self.state.no_trade_streak = int(fm.get("no_trade_streak") or 0)
        self.state.windows_seen = int(fm.get("windows_seen") or 0)
        for v in (fm.get("recent_sigmas") or []):
            self.state.recent_sigmas.append(float(v))
        for key, value in (fm.get("active_overrides") or {}).items():
            name, _, skill = key.partition("|")
            if isinstance(value, list):
                value = tuple(value)
            try:
                rm.set_override(name, value, skill=skill or None,
                                reason="restart re-apply", caller="tuner")
            except rm.RiskError as e:
                log.warning("stored override %s dropped on reload: %s", key, e)

    # --- per-tick entry point (orchestrator, after analyst.poll_pending) ---

    def on_reports(self, reports: list[PostmortemReport]) -> list[ParamAdjustment]:
        adjustments: list[ParamAdjustment] = []
        for report in reports:
            if report is None:
                continue
            adjustments.extend(tuner_skill.apply_feedback(self.state, report))
        for a in adjustments:
            self.changelog.append(a)
            self._notify(a)
        if reports:
            self._persist()
        return adjustments

    # --- helpers ---

    def _notify(self, a: ParamAdjustment) -> None:
        if self.discord is None:
            return
        label = f"{a.param}[{a.skill}]" if a.skill else a.param
        tighten = _is_tighten(a)
        arrow = "🔻 tightened" if tighten else "🔼 relaxed"
        self.discord.notify(
            f"⚙ **TUNER** — {arrow} `{label}`: {a.old_value} → {a.new_value}\n"
            f"Reason: {a.reason}",
            level="warning" if tighten else "info")

    def _persist(self) -> None:
        fm = {
            "loss_streak": self.state.loss_streak,
            "no_trade_streak": self.state.no_trade_streak,
            "windows_seen": self.state.windows_seen,
            "recent_sigmas": [round(s, 4) for s in self.state.recent_sigmas],
            "active_overrides": rm.active_overrides(),
            "env": self.env,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        lines = ["# Tuner State", "",
                 "| When (UTC) | Param | Skill | Old | New | Reason |",
                 "|---|---|---|---|---|---|"]
        for a in self.changelog:
            lines.append(f"| {a.ts:%Y-%m-%d %H:%M} | {a.param} | "
                         f"{a.skill or '—'} | {a.old_value} | {a.new_value} | "
                         f"{a.reason} |")
        try:
            self.vault.write_note(STATE_PATH, fm, "\n".join(lines) + "\n",
                                  caller="tuner")
        except Exception as e:
            log.error("tuner state persist failed: %s", e)
