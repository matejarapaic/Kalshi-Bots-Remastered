# discord-bot

**Trigger:** a trade needs approval or notification; a human issues a slash command; the system needs to escalate anything to its owner.

## What this is for

The human interface: trade cards with Approve/Reject buttons, slash commands for state inspection and the kill switch. It is **not on the critical trading path** — Discord being down must never crash or stall a trading cycle — with one deliberate, documented exception: in manual-approve mode, a *pending entry* blocks on approval by design (that's the entire point of manual-approve), while window monitoring and exits continue unaffected. Cards describe 15-minute crypto windows (market tickers like `KXBTC15M-26JUL222130-30`).

## Interface

```python
send_trade_card(card: TradeCard, timeout_s: float | None = None) -> ApprovalOutcome
    # blocks the calling (entry) thread until decision/timeout; timeout defaults
    # from card.is_live (see rule 2)
notify(text: str, level: Literal["info", "warn", "critical"]) -> None  # fire-and-forget, queued
handle_command(cmd: str, arg: str = "", user: str = "owner") -> str    # transport-agnostic
resolve_card(client_order_id: str, decision: str, user: str, authorized: bool) -> bool
    # called by the transport on button click; first decision wins
flush() -> None            # deliver queued messages; requeue on failure; called every orchestrator cycle
expire_all_pending() -> None   # in-place-reload reconciliation: releases all blocked entry waits
pending_count() -> int
# /positions /pnl /skills /window /halt <reason> /resume are REAL Discord slash
# commands (2026-07-17): registered + dispatched by kalshi_bots/discord_gateway.py
# (discord.py gateway client), calling handle_command() above. Button clicks
# (Approve/Reject) are handled the same way, via resolve_card().
```

Exceptions: `DiscordError` (base — also raised for a bad `MODE` or autonomous+prod at construction), `DiscordUnavailable` (send failed — meaningful only for approval cards; notify swallows it into the retry queue).

**Three transport implementations exist**, all send-compatible drop-ins (`send(message: dict) -> str`), in the preference order the orchestrator applies automatically:
- `GatewayTransport` (`kalshi_bots/discord_gateway.py`) — a real websocket connection (discord.py), running in its own asyncio loop on a background thread; `bind()` then `start()` before use (start raises `DiscordUnavailable` if not ready within `READY_TIMEOUT_S`). Receives and dispatches both slash commands and button clicks; renders cards as embeds with persistent `custom_id` buttons (`approve:<coid>` / `reject:<coid>`) parsed in a generic interaction handler — deliberately not per-message view callbacks, so a card posted before a process restart is still clickable. Required for `manual_approve` mode to be usable at all. Preferred whenever `DISCORD_BOT_TOKEN`+`DISCORD_CHANNEL_ID` are set; lazily imported so the system runs without discord.py installed.
- `DiscordTransport` (`kalshi_bots/skills/discord_bot.py`) — REST API only. Can send messages (truncated to Discord's 2000-char limit); **cannot** receive slash commands or button clicks (no persistent connection). 429s, HTTP errors, and network failures all raise `DiscordUnavailable`. Sufficient for autonomous-mode notifications. Fallback if the gateway can't connect or discord.py is missing.
- `ConsoleTransport` — local log only; approvals resolve only via `resolve_card` (dashboard/tests). Used when no Discord credentials are set.

## Behavior

### Execution mode — ANSWERED (owner, 2026-07-17): autonomous on DEMO only
1. **Owner decision 2026-07-17: `MODE=autonomous` while `KALSHI_ENV=demo`; prod requires re-answering the question** (enforced in `__init__`: the bot refuses autonomous+prod, with deliberately no override env var). Both modes remain specified:
   - `manual_approve`: order placed only on Approve click by an authorized role. Card expires after the approval timeout → the entry is **CANCELED, never auto-approved**, and the click on an expired card gets an ephemeral "already expired or decided."
   - `autonomous`: `send_trade_card` approves immediately with no Discord message of its own — it only gates whether the order is *allowed* to place. The actual "trade placed" notification is sent by the trader, only after a fill is confirmed, using the real fill price/quantity/fee (never the proposed sizing). **Spec correction (2026-07-17):** an earlier implementation notified from inside `send_trade_card`, before `place_order` ran — an order that ended up unfilled had already announced a trade that never happened. Fixed: notification now strictly follows a confirmed fill.
2. Timeouts: `APPROVAL_TIMEOUT_LIVE_S=120` (in-window edges decay in minutes), `APPROVAL_TIMEOUT_NON_LIVE_S=600` — both CONFIRMED 2026-07-17 (values category-agnostic and unchanged; the non-live constant was renamed in the crypto pivot, value untouched). `TradeCard.is_live` selects between them; an explicit `timeout_s` overrides.
3. **Exits are NEVER approval-gated**, in either mode. A blocked exit is unbounded risk; the trader's mechanical exits (including the universal near-close sweep) execute immediately and post notify-only messages. (Restated from the trader's system prompt; enforced here by having no exit-approval path at all.)

### Trade cards
4. Card content (`_render_card`, wrapped in an embed titled "Trade Proposal" by the gateway): skill name; side + action + contracts + market ticker (a 15-minute window, e.g. `KXBTC15M-26JUL222130-30`) + limit price; fee estimate and `capped_by` from `SizingResult`; **every entry-condition number that justified the trade** (the same snapshot the trade note records — supplied in `TradeCard.snapshot`, rendered as sorted key/value lines). Buttons: `Approve` / `Reject`. On a valid decision the embed recolors (green/red), gains a "Approved/Rejected by <user>" footer, and loses its buttons.
5. Button authorization: only members with `APPROVER_ROLE_ID` may click; other clicks get an ephemeral "not authorized" and the card stays live. Decisions are recorded (who, when) in `ApprovalOutcome` and on the edited card. **Spec deviation (2026-07-17):** if `APPROVER_ROLE_ID` is unset, the implementation fails OPEN (allows the click) rather than refusing to start — refusing to let anyone approve/halt because a role id was never configured was judged the worse failure mode for a single-operator system. `/halt` and `/resume` are gated by the same check (an unauthenticated halt/resume is the same class of risk); the other slash commands are read-only and ungated.
6. Idempotency: a card is bound to the trader's `client_order_id`; double-clicks or Discord retries cannot approve twice (first decision wins; late clicks after expiry or pop are no-ops).
7. **Current wiring status:** the approval path (card send, blocking wait, resolve, timeout) is fully implemented and tested, but has no live caller yet — the trader declines every `fair-value-candidate` with `declined:model_not_wired(sprint-3)` until the fair-value model lands. Sprint-3 wires entry proposals through `send_trade_card`.

### Blocking semantics (the documented exception)
8. `send_trade_card` blocks only its caller — the trader's entry path. Window monitoring, exit management, and other windows' signals never wait on Discord. If the card can't be delivered (`DiscordUnavailable`), the entry is abandoned (fail-closed, `decision="undeliverable"`) and a retry-queued warn notify records the miss.

### Reliability
9. Notifies go through an outbound queue; `flush()` (called every orchestrator cycle) delivers in order and requeues at the front on failure — delivery is retried each cycle until the transport recovers. Approval cards do NOT queue: they are sent directly, and a failed send is an immediate fail-closed abandon (rule 8). **Overflow policy** (queue ≥ `QUEUE_MAX=200`): drop the oldest droppable (non-critical notify) message; `critical` notifies always enqueue, growing the queue past max if they must; a non-critical enqueue into a queue full of undroppable messages raises `DiscordUnavailable`.
10. Discord outage in manual-approve mode ⇒ no approvals possible ⇒ no new entries; exits unaffected. Pending-card state is in-memory only, so a process restart has no pending cards to leak; `expire_all_pending()` exists for in-place reloads and releases every blocked entry wait as expired. In manual-approve, the trader re-proposes if the signal still verifies.

### Slash commands
11. `/positions` — open positions + `ExposureSummary` from risk-management, with per-event open-cost lines from `by_event` (one event ticker = one 15-minute window). `/pnl` — daily realized P&L, env-labeled (`env=demo|prod`). `/skills` — vault query of `02-trading-skills` notes with `status: confirmed` (the notes whose frontmatter carries `families`, e.g. `[KXBTC15M]`, and `signal_types`), showing `demo_win_rate`/`demo_sample_size`. `/window` — the most recently updated `03-market-context/active-windows` note: ticker, phase, strike, spot, sigma (crypto has no daily slate; the window resolves live from the clock every 15 minutes). `/halt <reason>` — `risk_management.set_halt(True, reason, caller="discord")` + a `critical` notify; halt state persists via the ledger note, so a restart stays halted; ack posted. `/resume` — clears halt + a `warn` notify; requires the same role. Unknown commands return `unknown command: <cmd>`.
12. Slash-command sync: instant when the guild is known (`DISCORD_GUILD_ID`, or auto-detected if the bot is in exactly one guild); otherwise global sync (~1hr propagation) with a logged warning.

### Postmortem rollups — PLANNED (sprint-4)
13. Window-close drives postmortems, and 96 windows/day would flood the channel one message per window. Sprint-4 adds batched rollups: an hourly digest notify, with individual postmortem messages only for windows in which a trade actually happened. Not implemented yet — nothing in this module batches today.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `MODE` | `manual_approve` (class default, via `KALSHI_EXEC_MODE`) | Owner-decided 2026-07-17: the orchestrator explicitly requests `autonomous`, allowed only on demo |
| `APPROVAL_TIMEOUT_LIVE_S` | 120 | CONFIRMED 2026-07-17 |
| `APPROVAL_TIMEOUT_NON_LIVE_S` | 600 | CONFIRMED 2026-07-17; renamed for the crypto pivot (2026-07-22), value unchanged |
| `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` | — | env vars |
| `DISCORD_GUILD_ID` | — | env var; optional — enables instant (vs. ~1hr global) slash-command sync. Auto-detected if the bot is in exactly one guild. |
| `APPROVER_ROLE_ID` | — | env var; unset = fails open (see rule 5 deviation), not a startup refusal |
| `QUEUE_MAX` | 200 | overflow threshold |
| `READY_TIMEOUT_S` | 25 | gateway startup readiness wait (operational, not a trading parameter); observed live: a fresh connect can take >15s under reconnect churn |

## Edge cases
- **Approve clicked at second N of an N-second timeout:** `resolve_card` compares against the card's monotonic-clock expiry; expiry wins ties (never place after cancel).
- **Card approved but order placement then fails** (`KalshiOrderRejected`): no silent retry of a money action — the failure is surfaced to the owner. (Contract for the sprint-3 entry wiring; the approval path exists today, its entry caller does not.)
- **Two pending cards for the same window** (different skills): the pending registry keys on `client_order_id`, so both display and resolve independently; one-position-per-window placement policy belongs to the trader, not this skill.
- **Process restarts with cards pending:** pending state is in-memory, so nothing survives; blocked entries died with the process, and in manual-approve the trader re-proposes if the signal still verifies. `expire_all_pending()` covers the in-place-reload case.
- **Rate limits from Discord:** a 429 raises `DiscordUnavailable`; queued notifies requeue and retry on the next `flush()`; a rate-limited approval card is an undeliverable entry (fail-closed, rule 8).
- **`/halt` during an in-flight approved order:** halt blocks new entries (the trader checks `risk.halted()` per signal); the in-flight order proceeds (it was approved pre-halt) — documented so nobody expects halt to claw back a click.

## Dependencies
risk-management (exposure, halt), vault (skill/window queries, halt persistence via ledger). Called by: trader (exit notifies now; entry cards from sprint-3), orchestrator + agents (notify/escalation; `flush()` each cycle; transport cascade at startup). Not on the critical path (rule 8's carve-out aside).

## Testing requirements
- Timeout → cancel, never place: clock-controlled test around the approval timeout.
- Role gating: unauthorized click no-ops; authorized approve returns outcome once (double-click idempotency).
- Overflow: queue at `QUEUE_MAX` drops oldest droppable; `critical` still enqueues.
- Outage: `send_trade_card` during simulated outage → `undeliverable` outcome → caller abandons entry; `flush()` requeues on outage and redelivers after recovery.
- Mode flip: autonomous mode approves without waiting and sends no notify of its own.
- Halt persistence: `/halt` → restart → still halted.
- Transport: send returns the Discord message id; truncation at 2000 chars; 429/HTTP error/network failure each raise `DiscordUnavailable`.

## New types
```python
@dataclass
class TradeCard:
    client_order_id: str; skill_name: str; market: MarketRef
    side: Side; action: Literal["buy", "sell"]
    sizing: SizingResult; snapshot: dict     # entry-condition numbers + timestamps
    is_live: bool                            # selects timeout (in-window vs. not)
@dataclass
class ApprovalOutcome:
    decision: Literal["approved", "rejected", "expired", "undeliverable"]
    decided_by: str | None; decided_at: datetime | None; card_message_id: str | None
```
