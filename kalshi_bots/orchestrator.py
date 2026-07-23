"""Orchestrator. Prompt: vault 00-meta/orchestrator-system-prompt.md.

Wires the three agents and runs the streaming loop. KALSHI_ENV=demo is
asserted at startup.

Streaming shape (crypto pivot): the exchange composite feed and the Kalshi
market-data WebSocket run as background asyncio tasks that keep in-memory
state current continuously; the main loop is a 1-second evaluation cadence
over that state — no HTTP polling on the hot path. (Design note: the loop
evaluates once per second rather than waking per WS message because every
entry/exit rule in this system gates on window phase and second-scale
staleness bounds, not on individual ticks; sub-second reaction adds
complexity with no consumer.)

24/7 operation: no off-season, no schedule, no daily slate. The window
monitor resolves the active 15-minute contract straight from the clock and
verifies it against the API every window.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

from kalshi_bots.agents.analyst import Analyst
from kalshi_bots.agents.trader import Trader
from kalshi_bots.agents.window_monitor import WindowMonitor
from kalshi_bots.env import load_env
from kalshi_bots.paper import PaperBroker
from kalshi_bots.skills.crypto_price_feed import CryptoPriceFeed
from kalshi_bots.skills.discord_bot import ConsoleTransport, DiscordBot, DiscordTransport
from kalshi_bots.skills.kalshi_client import KalshiClient
from kalshi_bots.skills.kalshi_ws_orderbook import KalshiOrderBook
from kalshi_bots.skills.risk_management import RiskManager
from kalshi_bots.skills.vault import Vault
from kalshi_bots.skills.window_monitor import WindowResolver

load_env()
log = logging.getLogger(__name__)

TICK_S = 1.0  # evaluation cadence over streaming state
LIVE_FLAG = "--i-know-what-im-doing-crypto"
LIVE_CONFIRM_PHRASE = "TRADE LIVE"


def live_trading_guard(vault, argv: list[str] | None = None,
                       confirm_input=None) -> str:
    """The paper-first rule, enforced at startup. Returns the run mode.

    KALSHI_ENV=demo -> "demo" (normal operation, no questions asked).
    KALSHI_ENV=prod requires EXEC_MODE=live AND all three of:
      1. the explicit `--i-know-what-im-doing-crypto` CLI flag,
      2. a positive interactive confirmation (typed phrase),
      3. at least one `confirmed`-status trading skill in the vault
         (draft skills never trade live money — this re-asserts the
         matcher's own gate independently).
    Any single missing -> SystemExit with a clear message. Even a full pass
    only *permits* startup: KalshiClient still refuses prod without
    KALSHI_ALLOW_PROD=yes-i-mean-it, and DiscordBot refuses autonomous on
    prod, so live trading is manual-approve by construction.
    """
    env = os.environ.get("KALSHI_ENV", "demo")
    if env == "demo":
        return "demo"
    if os.environ.get("EXEC_MODE") != "live":
        raise SystemExit(
            "KALSHI_ENV=prod refused: EXEC_MODE=live not set. Prod paper "
            "trading is not a thing — use KALSHI_ENV=demo.")
    argv = sys.argv[1:] if argv is None else argv
    if LIVE_FLAG not in argv:
        raise SystemExit(
            f"live crypto trading refused: missing the {LIVE_FLAG} CLI flag.")
    ask = confirm_input or input
    answer = ask(f"Type '{LIVE_CONFIRM_PHRASE}' to confirm real-money "
                 f"15-minute crypto trading: ")
    if (answer or "").strip() != LIVE_CONFIRM_PHRASE:
        raise SystemExit(
            "live crypto trading refused: interactive confirmation not given.")
    from kalshi_bots.types import VaultQuery
    notes = [n for n in vault.query(VaultQuery(directory="02-trading-skills"))
             if n.frontmatter.get("status") == "confirmed"
             and not n.path.endswith("_skill-template.md")]
    if not notes:
        raise SystemExit(
            "live crypto trading refused: no confirmed-status trading skill "
            "in the vault. Draft skills never trade live money — confirm one "
            "after >=30 settled demo samples and an owner review.")
    log.warning("LIVE TRADING GUARD PASSED: %d confirmed skill(s); execution "
                "will be manual-approve (autonomous is demo-only)", len(notes))
    return "live"


class Orchestrator:
    def __init__(self, series: str = "KXBTC15M",
                 paper: bool | None = None, vault: Vault | None = None):
        """paper=None auto-detects: real demo-exchange execution if
        KALSHI_KEY_ID/KALSHI_KEY_PATH authenticate successfully, else falls
        back to PaperBroker simulation. Pass True/False to force either mode
        regardless of credentials."""
        self.series = series
        self.vault = vault or Vault()
        # paper-first: demo passes straight through; prod demands the full
        # three-gate flow (flag + typed confirmation + confirmed skill) and
        # even then remains manual-approve + KALSHI_ALLOW_PROD-gated
        self.mode = live_trading_guard(self.vault)
        env = os.environ.get("KALSHI_ENV", "demo")
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
        self.paper = paper
        self.broker = PaperBroker(self.kalshi) if paper else self.kalshi
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
        # Owner decision 2026-07-22: autonomous on prod too, no approve/deny
        # step on entries (supersedes the prior demo-only restriction).
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

        # Streaming clients. The exchange composite always runs; the Kalshi WS
        # requires auth (there is no public channel), so paper mode without
        # credentials degrades to REST orderbook reads inside the trader —
        # fail-closed, never a fabricated stream.
        self.feed = CryptoPriceFeed()
        self.book: KalshiOrderBook | None = None
        try:
            self.kalshi.ws_auth_headers()
            self.book = KalshiOrderBook(self.kalshi)
        except Exception as e:
            log.warning("Kalshi WS unavailable (%s) — order books via REST "
                       "on demand", e)

        self.resolver = WindowResolver(self.kalshi, series=series)
        self.monitor = WindowMonitor(self.vault, self.resolver,
                                     book=self.book, feed=self.feed)
        self.trader = Trader(self.vault, self.broker, self.risk,
                             self.discord, env=self.mode,
                             feed=self.feed, book=self.book)
        try:
            counts = self.trader.reload_open_trades(self.risk.last_reconcile_settled)
            if counts["restored"] or counts["closed"]:
                log.info("restart recovery: restored %d open trade(s) for exit "
                         "management, closed %d that settled while down",
                         counts["restored"], counts["closed"])
        except Exception as e:
            log.warning("open-trade reload skipped: %s", e)
        self.analyst = Analyst(self.vault, self.broker, discord=self.discord,
                               env=self.mode,
                               paper_broker=self.broker if paper else None)
        self.events: list[dict] = []   # dashboard feed (bounded below)

    def _emit(self, kind: str, **data):
        for k, v in list(data.items()):
            if is_dataclass(v):
                data[k] = asdict(v)
        evt = {"kind": kind, "ts": datetime.now(timezone.utc).isoformat(), **data}
        self.events.append(evt)
        del self.events[:-500]
        log.info("%s %s", kind, {k: v for k, v in data.items() if k != "raw"})

    async def run_tick(self, now: datetime | None = None) -> dict:
        """One evaluation pass over streaming state."""
        now = now or datetime.now(timezone.utc)
        summary = {"signals": 0, "dispositions": [], "exits": [], "closes": []}
        try:
            signals = await self.monitor.tick(now)
        except Exception as e:
            log.error("monitor tick failed: %s", e)
            return summary
        for sig in signals:
            summary["signals"] += 1
            self._emit("signal", series=sig.series_ticker,
                       event_id=sig.window.event_ticker if sig.window else None,
                       signal_type=sig.signal_type, phase=sig.phase,
                       market_ticker=sig.market_ticker)
            if sig.signal_type == "window-close":
                summary["closes"].append(sig.market_ticker)
                try:
                    self.analyst.on_window_close(sig.window)
                except Exception as e:
                    log.error("postmortem failed: %s", e)
                continue
            if sig.signal_type == "fair-value-candidate":
                try:
                    disposition = self.trader.handle_signal(sig, now=now)
                except Exception as e:
                    log.error("trader signal handling failed: %s", e)
                    disposition = f"declined:handler_error({e})"
                summary["dispositions"].append(
                    {"signal": sig.signal_type,
                     "market_ticker": sig.market_ticker, "result": disposition})
                self._emit("disposition", series=sig.series_ticker,
                           market_ticker=sig.market_ticker,
                           signal_type=sig.signal_type, result=disposition)
        try:
            summary["exits"].extend(self.trader.manage_positions(now))
        except Exception as e:
            log.error("exit sweep failed: %s", e)
        try:
            for report in self.analyst.poll_pending(now):
                if report is not None:
                    self._emit("postmortem", event_id=report.event_id,
                               settlement=report.settlement_status,
                               trades=report.trades_audited,
                               pnl_cents=report.realized_pnl_cents)
        except Exception as e:
            log.error("settlement poll failed: %s", e)
        self.discord.flush()
        return summary

    async def _run_async(self, cycles: int | None = None, tick_s: float = TICK_S):
        await self.feed.start()
        if self.book is not None:
            await self.book.start()
        try:
            n = 0
            while cycles is None or n < cycles:
                await asyncio.sleep(tick_s)
                summary = await self.run_tick()
                if summary["signals"] or summary["exits"]:
                    self._emit("cycle", n=n,
                               **{k: v for k, v in summary.items()
                                  if k != "dispositions"})
                n += 1
            return summary
        finally:
            if self.book is not None:
                await self.book.stop()
            await self.feed.stop()

    def run(self, cycles: int | None = None, poll_s: float | None = None):
        """Bounded (smoke) or unbounded run loop. `poll_s` is honored as the
        tick cadence for signature compatibility with the old poll loop."""
        return asyncio.run(self._run_async(cycles, tick_s=poll_s or TICK_S))
