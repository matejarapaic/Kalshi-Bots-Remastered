"""analyst agent. Prompt: vault 01-agents/analyst/system-prompt.md.

Runs postmortems on window-close, updates skill stats (sole writer). Never on
the live path.

Sprint-2 state: window-close handling logs and returns — the 15-minute-cadence
postmortem (batched rollups, daily aggregate notes, paper settlement from the
window log) is sprint-4's scope and is wired there, not half-wired here.
"""
from __future__ import annotations

import logging

from kalshi_bots.skills.postmortem import Postmortem
from kalshi_bots.types import WindowRef

log = logging.getLogger(__name__)


class Analyst:
    def __init__(self, vault, broker, discord=None, env: str = "demo",
                 paper_broker=None):
        self.vault = vault
        self.broker = broker
        self.discord = discord
        self.paper = paper_broker
        self.postmortem = Postmortem(vault, broker, discord_bot=discord, env=env)

    def on_window_close(self, window: WindowRef):
        """Called by the orchestrator on every window-close signal.
        Sprint-4 wires: paper settlement from the window's BRTI log, the
        mechanical audit with crypto counterfactuals, batched Discord rollups,
        and daily-aggregate postmortem notes."""
        log.info("window closed: %s (postmortem cadence lands sprint-4)",
                 window.market_ticker)
        return None
