"""Orchestrator. Prompt: vault 00-meta/orchestrator-system-prompt.md.

Wires the three agents and runs the polling loop. Scheduling policy comes from
league-config's ramp rules; this build implements the live-game cadence and a
bounded run mode for paper cycles. KALSHI_ENV=demo is asserted at startup.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from kalshi_bots.agents.analyst import Analyst
from kalshi_bots.agents.game_monitor import GameMonitor
from kalshi_bots.agents.trader import Trader
from kalshi_bots.env import load_env
from kalshi_bots.paper import PaperBroker
from kalshi_bots.skills.discord_bot import ConsoleTransport, DiscordBot, DiscordTransport
from kalshi_bots.skills.espn_data import EspnData
from kalshi_bots.skills.kalshi_client import KalshiClient
from kalshi_bots.skills.league_matching import LeagueMatcher
from kalshi_bots.skills.odds_api import OddsApi
from kalshi_bots.skills.risk_management import RiskManager
from kalshi_bots.skills.vault import Vault

load_env()
log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

LIVE_POLL_S = 20  # league-config ramp: live cadence


class Orchestrator:
    def __init__(self, leagues: list[str] | None = None,
                 paper: bool | None = None, vault: Vault | None = None):
        """paper=None auto-detects: real demo-exchange execution if
        KALSHI_KEY_ID/KALSHI_KEY_PATH authenticate successfully, else falls
        back to PaperBroker simulation. Pass True/False to force either mode
        regardless of credentials."""
        env = os.environ.get("KALSHI_ENV", "demo")
        if env != "demo":
            raise RuntimeError("orchestrator refuses to start: KALSHI_ENV must be "
                               "demo until the Phase 3 final checkpoint")
        self.vault = vault or Vault()
        self.kalshi = KalshiClient()
        if paper is None:
            try:
                self.kalshi.get_balance()
                paper = False
                log.info("Kalshi demo credentials verified — using real "
                        "demo-exchange execution")
            except Exception as e:
                paper = True
                log.warning("Kalshi demo auth unavailable (%s) — falling back "
                           "to PaperBroker simulation", e)
        self.broker = PaperBroker(self.kalshi) if paper else self.kalshi
        self.espn = EspnData(self.vault)
        self.matcher = LeagueMatcher(self.vault, self.kalshi)
        # odds-api is optional: divergence/injury skills stay dormant (no
        # candidates emitted) without a key, rather than failing startup.
        self.odds = OddsApi(self.vault) if os.environ.get("ODDS_API_KEY") else None
        self.risk = RiskManager(self.vault, self.broker)
        try:
            self.risk.reconcile()
        except Exception as e:
            log.warning("startup ledger reconcile skipped: %s", e)
        # Execution mode, owner-decided 2026-07-17: AUTONOMOUS ON DEMO ONLY.
        # This orchestrator refuses non-demo envs at startup (above), and
        # DiscordBot separately refuses autonomous+prod — flipping to prod
        # requires re-answering the execution-mode question.
        #
        # Transport cascade: GatewayTransport (full — sends + receives slash
        # commands/button clicks) > DiscordTransport (REST, send-only, if the
        # gateway can't connect) > ConsoleTransport (local log only).
        discord_token = os.environ.get("DISCORD_BOT_TOKEN")
        discord_channel = os.environ.get("DISCORD_CHANNEL_ID")
        discord_guild = os.environ.get("DISCORD_GUILD_ID")
        gateway = None
        if discord_token and discord_channel:
            try:
                from kalshi_bots.discord_gateway import GatewayTransport  # optional [discord] extra
                gateway = GatewayTransport(discord_token, discord_channel, discord_guild)
            except ImportError as e:
                log.warning("discord.py not installed (%s) — falling back to "
                           "REST-only transport; `pip install discord.py` for "
                           "slash commands/buttons", e)
        transport = gateway or (DiscordTransport(discord_token, discord_channel)
                                if discord_token and discord_channel else ConsoleTransport())
        self.discord = DiscordBot(self.risk, self.vault, transport=transport,
                                  mode="autonomous")
        if gateway is not None:
            gateway.bind(self.discord)  # bind before start: no window with bot_ref unset
            try:
                gateway.start()
                log.info("Discord gateway connected — slash commands and "
                        "approval buttons are live")
            except Exception as e:
                log.warning("Discord gateway failed to connect (%s) — falling "
                           "back to REST-only transport", e)
                self.discord.transport = DiscordTransport(discord_token, discord_channel)
        self.monitor = GameMonitor(self.vault, self.espn, self.matcher,
                                   kalshi=self.kalshi, odds=self.odds)
        self.trader = Trader(self.vault, self.broker, self.matcher, self.risk,
                             self.discord, env="demo")
        self.analyst = Analyst(self.vault, self.broker, self.espn,
                               discord=self.discord, env="demo",
                               paper_broker=self.broker if paper else None)
        self.leagues = leagues or ["mlb"]
        self.events: list[dict] = []   # dashboard feed

    def _emit(self, kind: str, **data):
        for k, v in list(data.items()):
            if is_dataclass(v):
                data[k] = asdict(v)
        evt = {"kind": kind, "ts": datetime.now(timezone.utc).isoformat(), **data}
        self.events.append(evt)
        del self.events[:-500]
        log.info("%s %s", kind, {k: v for k, v in data.items() if k != "raw"})

    def run_cycle(self, day: date | None = None) -> dict:
        """One full poll->signal->trade->exit pass across all leagues."""
        day = day or datetime.now(timezone.utc).astimezone(ET).date()
        summary = {"signals": 0, "dispositions": [], "exits": [], "finals": []}
        for league in self.leagues:
            signals = self.monitor.poll_cycle(league, day)
            games = {}
            try:
                games = {g.espn_event_id: g
                         for g in self.espn.get_scoreboard(league)}
            except Exception as e:
                log.error("scoreboard refetch failed: %s", e)
            for sig in signals:
                summary["signals"] += 1
                self._emit("signal", league=sig.league, sport=sig.league,
                           game_id=sig.espn_event_id,
                           signal_type=sig.signal_type,
                           market_ticker=sig.market_ticker)
                if sig.signal_type == "game-final":
                    summary["finals"].append(sig.espn_event_id)
                    try:
                        self.analyst.on_game_final(league, sig.espn_event_id)
                    except Exception as e:
                        log.error("postmortem failed: %s", e)
                    continue
                game = games.get(sig.espn_event_id)
                if game is None:
                    continue
                disposition = self.trader.handle_signal(sig, game)
                summary["dispositions"].append(
                    {"signal": sig.signal_type, "game_id": sig.espn_event_id,
                     "result": disposition})
                self._emit("disposition", league=league, sport=league,
                           game_id=sig.espn_event_id,
                           signal_type=sig.signal_type, result=disposition)
            summary["exits"].extend(self.trader.manage_positions(games))
        self.discord.flush()
        return summary

    def run(self, cycles: int | None = None, poll_s: int = LIVE_POLL_S):
        """Bounded (paper) or unbounded run loop."""
        day = datetime.now(timezone.utc).astimezone(ET).date()
        for league in self.leagues:
            self.monitor.build_slate(league, day)
        n = 0
        while cycles is None or n < cycles:
            started = time.monotonic()
            summary = self.run_cycle(day)
            self._emit("cycle", n=n, **{k: v for k, v in summary.items()
                                        if k != "dispositions"})
            n += 1
            if cycles is not None and n >= cycles:
                return summary
            time.sleep(max(0, poll_s - (time.monotonic() - started)))
