"""analyst agent. Prompt: vault 01-agents/analyst/system-prompt.md.

Runs postmortems on game-final, updates skill stats (sole writer), builds the
next-day slate preview. Never on the live path.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from kalshi_bots.skills.postmortem import Postmortem, SettlementMismatch
from kalshi_bots.timefmt import fmt_et

log = logging.getLogger(__name__)


class Analyst:
    def __init__(self, vault, kalshi, espn, discord=None, env: str = "demo",
                 paper_broker=None):
        self.vault = vault
        self.espn = espn
        self.discord = discord
        self.paper = paper_broker
        self.postmortem = Postmortem(vault, kalshi, discord_bot=discord, env=env)

    def on_game_final(self, league: str, espn_event_id: str):
        # paper mode: settle simulated positions from the ESPN final first
        if self.paper is not None:
            try:
                note = self.vault.read_note(
                    f"03-market-context/active-games/{league}-{espn_event_id}.md")
                fm = note.frontmatter
                ticker = fm.get("market_ticker")
                if ticker and fm.get("winner_kalshi_abbr"):
                    result = ("yes" if fm["winner_kalshi_abbr"] == fm.get("yes_team_kalshi_abbr")
                              else "no")
                    self.paper.settle(ticker, result)
            except Exception as e:
                log.warning("paper settlement skipped: %s", e)
        try:
            report = self.postmortem.run(league, espn_event_id)
            log.info("postmortem %s-%s: %d trades, %d declined, pnl %dc",
                     league, espn_event_id, report.trades_audited,
                     report.declined_candidates, report.realized_pnl_cents)
            return report
        except SettlementMismatch as e:
            log.critical("SETTLEMENT MISMATCH: %s", e)
            raise

    def nightly_slate(self, leagues: list[str], for_day: date | None = None):
        """Next-day slate preview for game-monitor."""
        day = for_day or (date.today() + timedelta(days=1))
        lines = [f"# Slate preview {day.isoformat()}", ""]
        for league in leagues:
            try:
                games = self.espn.get_scoreboard(league)
            except Exception as e:
                lines.append(f"- {league}: scoreboard unavailable ({e})")
                continue
            upcoming = [g for g in games if g.start_time.date() == day]
            lines.append(f"## {league.upper()} — {len(upcoming)} games")
            for g in upcoming:
                lines.append(f"- {g.away.espn_abbr} @ {g.home.espn_abbr} "
                             f"{fmt_et(g.start_time)}")
        self.vault.write_note(
            f"03-market-context/daily-slate/{day.isoformat()}-preview.md",
            {"date": day.isoformat(), "leagues": leagues},
            "\n".join(lines) + "\n", caller="analyst")
