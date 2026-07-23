"""discord-bot skill. Spec: skills/discord-bot/SKILL.md.

Trade cards with Approve/Reject, slash commands, kill switch. Transport is
abstracted: DiscordTransport (real, needs discord.py + token) or any object
implementing the same interface (console/test transports) — the approval,
timeout, queue, and idempotency logic here is transport-independent.

Execution mode: autonomous, on demo and prod alike (owner re-decided
2026-07-22 — the prior demo-only restriction required re-answering this
question in code, not flipping a flag; this edit is that re-answer). This
class's own default is still the conservative `manual_approve` (see MODE
below) for anything that constructs it without an explicit mode, but the
orchestrator always requests `autonomous`. Expiry CANCELS, never
auto-approves. Exits are never approval-gated (there is no exit-approval path
at all).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests

from kalshi_bots.types import ApprovalOutcome, TradeCard

log = logging.getLogger(__name__)

APPROVAL_TIMEOUT_LIVE_S = 120     # owner-confirmed 2026-07-17
APPROVAL_TIMEOUT_NON_LIVE_S = 600  # owner-confirmed 2026-07-17
QUEUE_MAX = 200


class DiscordError(Exception):
    pass


class DiscordUnavailable(DiscordError):
    pass


class ConsoleTransport:
    """Fallback transport: logs messages; approvals resolve only via
    resolve_card (dashboard/tests). Used for paper cycles without a bot token."""

    def __init__(self):
        self.sent: list[dict] = []
        self.available = True

    def send(self, message: dict) -> str:
        if not self.available:
            raise DiscordUnavailable("transport down")
        self.sent.append(message)
        msg_id = f"console-{len(self.sent)}"
        log.info("[%s] %s", message.get("level", "card"), message.get("text", "")[:200])
        return msg_id


class DiscordTransport:
    """Real transport: posts messages via Discord's REST API using the bot
    token. Send-only — there is no gateway (websocket) connection here, so
    button clicks and slash-command invocations are NOT received; approval
    cards render as plain text and `resolve_card` must still be driven some
    other way (dashboard/console). Sufficient for autonomous-mode
    notifications (trade fills, halts, critical alerts); manual-approve mode
    needs real interactivity to be useful — see GatewayTransport in
    kalshi_bots/discord_gateway.py (a gateway-based transport, added
    2026-07-17), which the orchestrator prefers whenever credentials are
    present, falling back to this REST-only transport if the gateway can't
    connect.
    """

    API = "https://discord.com/api/v10"
    DISCORD_MAX_LEN = 2000

    def __init__(self, token: str, channel_id: str,
                 session: requests.Session | None = None):
        self.channel_id = channel_id
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bot {token}"

    def send(self, message: dict) -> str:
        text = message.get("text", "")[: self.DISCORD_MAX_LEN]
        try:
            r = self.session.post(
                f"{self.API}/channels/{self.channel_id}/messages",
                json={"content": text}, timeout=15)
        except requests.RequestException as e:
            raise DiscordUnavailable(f"request failed: {e}") from e
        if r.status_code == 429:
            raise DiscordUnavailable("rate limited")
        if r.status_code >= 300:
            raise DiscordUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")
        return str(r.json()["id"])


class DiscordBot:
    def __init__(self, risk_manager, vault, transport=None, mode: str | None = None):
        self.risk = risk_manager
        self.vault = vault
        self.transport = transport or ConsoleTransport()
        self.mode = mode or os.environ.get("KALSHI_EXEC_MODE", "manual_approve")
        if self.mode not in ("manual_approve", "autonomous"):
            raise DiscordError(f"bad MODE {self.mode!r}")
        # Owner decision 2026-07-22: autonomous authorized for prod, no human
        # approval on entries. Supersedes the 2026-07-17 demo-only restriction.
        self._pending: dict[str, dict] = {}   # client_order_id -> state
        self._plock = threading.Lock()
        self._queue: deque = deque()
        self._qlock = threading.Lock()

    # --- outbound queue (retry handled by caller loop; overflow policy here) ---

    def _enqueue(self, message: dict, critical: bool = False):
        with self._qlock:
            if len(self._queue) >= QUEUE_MAX:
                if not critical:
                    # drop oldest notify-only; never drop cards/halt-acks/critical
                    for i, m in enumerate(self._queue):
                        if m.get("droppable"):
                            del self._queue[i]
                            break
                    else:
                        raise DiscordUnavailable("queue full of undroppable messages")
                # critical always enqueues (queue grows past max if it must)
            self._queue.append(message)

    def flush(self):
        """Deliver queued messages; requeue on failure (called by the agent loop)."""
        while True:
            with self._qlock:
                if not self._queue:
                    return
                msg = self._queue.popleft()
            try:
                self.transport.send(msg)
            except DiscordUnavailable:
                with self._qlock:
                    self._queue.appendleft(msg)
                return

    # --- notifications ---

    def notify(self, text: str, level: str = "info") -> None:
        self._enqueue({"kind": "notify", "text": text, "level": level,
                       "droppable": level != "critical"},
                      critical=(level == "critical"))
        self.flush()

    # --- trade cards ---

    def send_trade_card(self, card: TradeCard,
                        timeout_s: float | None = None) -> ApprovalOutcome:
        if self.mode == "autonomous":
            # No notify here — this only decides whether the order is allowed
            # to place. The actual "trade placed" notification (real fill
            # price/quantity, not this proposal) is the trader's job, sent
            # only once a fill is confirmed. Bug found 2026-07-17: this used
            # to notify *before* place_order ran, so an order that ended up
            # unfilled had already announced a trade that never happened.
            return ApprovalOutcome(decision="approved", decided_by="autonomous",
                                   decided_at=datetime.now(timezone.utc),
                                   card_message_id=None)

        timeout_s = timeout_s if timeout_s is not None else (
            APPROVAL_TIMEOUT_LIVE_S if card.is_live else APPROVAL_TIMEOUT_NON_LIVE_S)
        payload = {
            "kind": "trade_card", "client_order_id": card.client_order_id,
            "text": self._render_card(card), "droppable": False,
        }
        try:
            msg_id = self.transport.send(payload)
        except DiscordUnavailable:
            # fail-closed: entry abandoned; record the miss as a queued notify
            self._enqueue({"kind": "notify", "level": "warn", "droppable": True,
                           "text": f"trade card undeliverable — entry abandoned: "
                                   f"{card.market.market_ticker}"})
            return ApprovalOutcome(decision="undeliverable", decided_by=None,
                                   decided_at=None, card_message_id=None)

        state = {"event": threading.Event(), "outcome": None,
                 "expires": time.monotonic() + timeout_s, "msg_id": msg_id}
        with self._plock:
            self._pending[card.client_order_id] = state
        state["event"].wait(timeout=timeout_s)
        with self._plock:
            self._pending.pop(card.client_order_id, None)
            if state["outcome"] is not None:
                return state["outcome"]
        # expiry wins ties: never place after cancel
        return ApprovalOutcome(decision="expired", decided_by=None,
                               decided_at=datetime.now(timezone.utc),
                               card_message_id=msg_id)

    def resolve_card(self, client_order_id: str, decision: str,
                     user: str, authorized: bool) -> bool:
        """Called by the transport on button click. First decision wins;
        unauthorized clicks no-op; late clicks (post-expiry/pop) no-op."""
        if not authorized:
            log.info("unauthorized approval click by %s ignored", user)
            return False
        if decision not in ("approved", "rejected"):
            return False
        with self._plock:
            state = self._pending.get(client_order_id)
            if state is None or state["outcome"] is not None:
                return False  # already decided/expired — idempotent
            if time.monotonic() >= state["expires"]:
                return False  # expiry wins ties
            state["outcome"] = ApprovalOutcome(
                decision=decision, decided_by=user,
                decided_at=datetime.now(timezone.utc),
                card_message_id=state["msg_id"])
            state["event"].set()
            return True

    def pending_count(self) -> int:
        with self._plock:
            return len(self._pending)

    def expire_all_pending(self):
        """Startup reconciliation: expire cards pending from before a restart."""
        with self._plock:
            for state in self._pending.values():
                state["event"].set()
            self._pending.clear()

    @staticmethod
    def _render_card(card: TradeCard) -> str:
        snap = "\n".join(f"  {k}: {v}" for k, v in sorted(card.snapshot.items()))
        return (f"TRADE CARD [{card.skill_name}]\n"
                f"{card.action.upper()} {card.side} {card.sizing.contracts}x "
                f"{card.market.market_ticker} @ {card.sizing.limit_price}c "
                f"(fee est {card.sizing.est_fee_cents_total}c, "
                f"capped_by={card.sizing.capped_by})\n"
                f"entry snapshot:\n{snap}")

    # --- slash commands (transport-agnostic handlers) ---

    def handle_command(self, cmd: str, arg: str = "", user: str = "owner") -> str:
        if cmd == "positions":
            e = self.risk.exposure()
            lines = [f"open positions: {e.open_positions}, "
                     f"cost {e.open_cost_cents}c / bankroll {e.bankroll_cents}c"]
            lines += [f"  {g}: {c}c" for g, c in e.by_event.items()]
            return "\n".join(lines)
        if cmd == "pnl":
            e = self.risk.exposure()
            return (f"daily realized: {e.daily_realized_pnl_cents}c "
                    f"(env={os.environ.get('KALSHI_ENV', 'demo')})")
        if cmd == "skills":
            from kalshi_bots.types import VaultQuery
            notes = self.vault.query(VaultQuery(directory="02-trading-skills",
                                                frontmatter_filters={"status": "confirmed"}))
            return "\n".join(
                f"{n.frontmatter['skill']}: wr={n.frontmatter.get('demo_win_rate')} "
                f"n={n.frontmatter.get('demo_sample_size', 0)}" for n in notes)
        if cmd == "halt":
            self.risk.set_halt(True, arg or "manual", caller="discord")
            self.notify(f"HALTED by {user}: {arg or 'manual'}", level="critical")
            return "halted (persists across restart)"
        if cmd == "resume":
            self.risk.set_halt(False, "", caller="discord")
            self.notify(f"resumed by {user}", level="warn")
            return "resumed"
        if cmd == "window":
            from kalshi_bots.types import VaultQuery
            notes = self.vault.query(VaultQuery(directory="03-market-context/active-windows"))
            if not notes:
                return "no active window note yet"
            note = max(notes, key=lambda n: n.frontmatter.get("updated", ""))
            fm = note.frontmatter
            return (f"{fm.get('market_ticker')} [{fm.get('phase')}] "
                    f"strike={fm.get('strike')} spot={fm.get('spot')} "
                    f"sigma={fm.get('sigma')}")
        return f"unknown command: {cmd}"
