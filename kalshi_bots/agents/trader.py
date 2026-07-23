"""trader agent. Prompt: vault 01-agents/trader/system-prompt.md.

The only component that places orders. Re-verifies every candidate with fresh
data (the monitor flags, it does not vouch), matches via skill-matcher, sizes
via risk-management, routes through Discord approval, executes limit orders,
records trade notes in the same cycle as the fill.

Sprint-2 state: signal handling declines everything with
"declined:model_not_wired" until the fair-value model lands (sprint-3).
Restart recovery and the universal near-close exit sweep are live — a 15-min
contract position is never held into settlement noise regardless of skill.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from datetime import timedelta

from kalshi_bots.skills.skill_matcher import SkillMatcher
from kalshi_bots.skills.window_monitor import (
    WINDOW_S, parse_market_ticker, window_phase,
)
from kalshi_bots.types import (
    CryptoSignal, OrderRequest, Settlement, VaultQuery, WindowRef,
)

log = logging.getLogger(__name__)


class Trader:
    def __init__(self, vault, broker, risk, discord, env: str = "demo"):
        self.vault = vault
        self.broker = broker           # KalshiClient or PaperBroker
        self.skill_matcher = SkillMatcher(vault)
        self.risk = risk
        self.discord = discord
        self.env = env
        self.open_trades: dict[str, dict] = {}    # client_order_id -> trade meta

    # --- restart recovery ---

    def reload_open_trades(self, settled: dict[str, Settlement] | None = None) -> dict[str, int]:
        """Repopulate open_trades from vault trade notes still marked
        status=open, so exit management resumes after an abrupt restart
        instead of silently going dark (open_trades is otherwise pure
        in-memory). `settled` is RiskManager.reconcile()'s
        last_reconcile_settled — tickers it discovered had settled while the
        process was down; those trade notes are closed out here (using the
        note's own recorded contracts/entry price, the same math postmortem
        uses) instead of being restored for exit management."""
        settled = settled or {}
        notes = self.vault.query(VaultQuery(directory="04-trade-history/trades",
                                            frontmatter_filters={"status": "open"}))
        restored = closed = 0
        for note in notes:
            fm = note.frontmatter
            coid = fm.get("client_order_id")
            ticker = fm.get("market_ticker")
            if not coid or not ticker:
                continue
            s = settled.get(ticker)
            if s is not None:
                contracts = fm.get("contracts", 0)
                cost = contracts * fm.get("entry_price_cents", 0) + fm.get("fee_cents", 0)
                won = s.result == fm.get("side")
                pnl = (contracts * 100 - cost) if won else -cost
                self._close_trade_note(note.path, "settled_while_down", None, pnl)
                closed += 1
                continue
            try:
                market = self.broker.get_market(ticker, family=fm.get("family", ""))
            except Exception as e:
                log.error("reload_open_trades: market fetch failed for %s: %s", ticker, e)
                continue
            opened_at = fm.get("opened_at")
            # rebuild the WindowRef from the ticker grammar so the near-close
            # exit sweep covers restored positions too (otherwise a restart
            # would orphan them until settlement)
            window = None
            try:
                series, closes_at = parse_market_ticker(ticker)
                window = WindowRef(
                    series_ticker=series,
                    event_ticker=ticker.rsplit("-", 1)[0], market_ticker=ticker,
                    opens_at=closes_at - timedelta(seconds=WINDOW_S),
                    closes_at=closes_at)
            except Exception as e:
                log.warning("reload: no window for %s (%s) — near-close exit "
                            "sweep will not cover it", ticker, e)
            self.open_trades[coid] = {
                "market_ticker": ticker, "skill": fm.get("skill"), "side": fm.get("side"),
                "contracts": fm.get("contracts", 0), "entry_price": fm.get("entry_price_cents"),
                "event_id": fm.get("event_id"), "family": fm.get("family"),
                "opened_at": datetime.fromisoformat(opened_at) if opened_at
                            else datetime.now(timezone.utc),
                "note": note.path, "market": market, "window": window,
            }
            restored += 1
        return {"restored": restored, "closed": closed}

    # --- signal handling ---

    def handle_signal(self, signal: CryptoSignal) -> str:
        """Returns a disposition string (for logs/postmortem declined reasons)."""
        if signal.signal_type != "fair-value-candidate":
            return "not-a-trade-signal"
        if signal.market_ticker is None:
            return "declined:unresolved_window"
        halted, reason = self.risk.halted()
        if halted:
            return f"declined:halted({reason})"
        # sprint-3 wires: fresh FairValueEstimate re-verification, skill
        # matching, sizing, approval, execution. Until then: never trade.
        return "declined:model_not_wired(sprint-3)"

    # --- exit management ---

    def manage_positions(self, now: datetime | None = None) -> list[str]:
        """Evaluate exit rules for every open trade. Exits are mechanical and
        never approval-gated. Sprint-2 implements the universal 15-minute
        contract rule: a position whose window has entered near_close (or
        later) is exited at market — no skill has settlement-noise edge.
        Edge-based exits arrive with the model (sprint-3)."""
        now = now or datetime.now(timezone.utc)
        actions = []
        for coid, t in list(self.open_trades.items()):
            window: WindowRef | None = t.get("window")
            reason = None
            if window is not None and window_phase(now, window) in ("near_close", "settled"):
                reason = "near_close_exit"
            if reason is None:
                continue
            market = t["market"]
            try:
                snapshot = self.broker.get_orderbook(market)
            except Exception as e:
                log.error("exit orderbook fetch failed: %s", e)
                continue
            bid = snapshot.yes_bid if t["side"] == "yes" else snapshot.no_bid
            if bid is None:
                log.error("no bid to exit into for %s — will retry", coid)
                continue
            try:
                order = self.broker.place_order(OrderRequest(
                    market_ticker=market.market_ticker, side=t["side"],
                    action="sell", contracts=t["contracts"], limit_price=max(1, bid - 1),
                    # unique per attempt: a fixed f"{coid}-exit" id collided
                    # ("order_already_exists") on retry after an attempt that
                    # didn't fully fill, killing the orchestrator loop
                    client_order_id=f"{coid}-exit-{uuid.uuid4().hex[:8]}"))
            except Exception as e:
                log.error("exit order failed for %s: %s — will retry", coid, e)
                continue
            if order.filled_contracts > 0:
                fills = [f for f in self.broker.get_fills(market.market_ticker)
                         if f.order_id == order.order_id]
                if fills:
                    self.risk.on_exit(fills[-1], market, t["skill"])
                pnl = (order.avg_fill_price - t["entry_price"]) * order.filled_contracts
                self._close_trade_note(t["note"], reason, order.avg_fill_price, pnl)
                self.discord.notify(
                    f"EXIT [{t['skill']}] {market.market_ticker} {reason}: "
                    f"{order.filled_contracts}x @ {order.avg_fill_price}c", "info")
                self.open_trades.pop(coid, None)
                actions.append(f"exited:{coid}:{reason}")
        return actions

    # --- trade notes ---

    def _note_path(self, coid: str) -> str:
        day = datetime.now(timezone.utc).date().isoformat()
        return f"04-trade-history/trades/{day}-{coid}.md"

    def _write_trade_note(self, coid, skill, market, side, order, signal_price,
                          model_prob, conditions, signal):
        path = self._note_path(coid)
        fm = {
            "client_order_id": coid,
            "event_id": signal.window.event_ticker if signal.window else "",
            "family": signal.series_ticker,
            "market_ticker": market.market_ticker,
            "skill": skill, "side": side,
            "contracts": order.filled_contracts,
            "entry_price_cents": order.avg_fill_price or signal_price,
            "signal_price_cents": signal_price,
            "fee_cents": order.fee_cents,
            "model_prob": model_prob,
            "entry_conditions": {k: bool(v) for k, v in conditions.items()},
            "signal_id": signal.payload.get("id"),
            "status": "open", "realized_pnl_cents": None,
            "exit_deviation": False,
            "env": self.env,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        body = (f"# Trade {coid}\n\n{skill} {side} {order.filled_contracts}x "
                f"{market.market_ticker} @ {order.avg_fill_price}c "
                f"(signal price {signal_price}c)\n")
        self.vault.write_note(path, fm, body, caller="trader")

    def _close_trade_note(self, path, reason, exit_price, pnl):
        try:
            self.vault.update_frontmatter(path, {
                "status": "closed", "exit_reason": reason,
                "exit_price_cents": exit_price, "realized_pnl_cents": pnl,
            }, caller="trader")
        except Exception as e:
            log.error("failed to close trade note %s: %s", path, e)
