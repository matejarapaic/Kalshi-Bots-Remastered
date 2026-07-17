# discord-bot

**Trigger:** a trade needs approval or notification; a human issues a slash command; the system needs to escalate anything to its owner.

## What this is for

The human interface: rich-embed trade cards with Approve/Reject buttons, slash commands for state inspection and the kill switch. It is **not on the critical trading path** — Discord being down must never crash or stall a trading cycle — with one deliberate, documented exception: in manual-approve mode, a *pending entry* blocks on approval by design (that's the entire point of manual-approve), while monitoring and exits continue unaffected.

## Interface

```python
send_trade_card(card: TradeCard) -> ApprovalOutcome   # blocks (async) until decision/timeout
notify(message: str, level: Literal["info", "warn", "critical"]) -> None  # fire-and-forget
# Slash commands (Discord-side handlers, not Python callables):
#   /positions  /pnl  /skills  /slate  /halt <reason>  /resume
```

Exceptions: `DiscordError` (base), `DiscordUnavailable` (send failed after retries — meaningful only for approval cards; notify swallows it into the retry queue).

## Behavior

### Execution mode — Category B, still OPEN
1. **Manual-approve vs. autonomous is an unanswered owner decision.** Until it is explicitly answered, `MODE=manual_approve` and ALL orders — demo included — require card approval. Both modes are specified so flipping is one config change:
   - `manual_approve`: order placed only on Approve click by an authorized role. Card expires after `APPROVAL_TIMEOUT_S` → the entry is **CANCELED, never auto-approved**, and the card edits to "expired."
   - `autonomous`: cards become notify-only (posted after placement, no buttons).
2. Timeouts (owner-confirmed 2026-07-17): `APPROVAL_TIMEOUT_LIVE_S=120` (live edges decay in minutes), `APPROVAL_TIMEOUT_PREGAME_S=600`.
3. **Exits are NEVER approval-gated**, in either mode. A blocked exit is unbounded risk; invalidation-triggered exits execute immediately and post notify-only cards. (Restated from the trader's system prompt; enforced here by having no exit-approval path at all.)

### Trade cards
4. Embed fields: skill name; market ticker + title; side + action; **every entry-condition number that justified the trade, with its timestamp** (the same snapshot the trade note records — supplied in `TradeCard.snapshot`); proposed contracts, limit price, fee estimate, `capped_by` from SizingResult; current book (bid/ask/depth); edge summary. Buttons: `Approve` / `Reject`, plus `Halt system` on `critical` notifies.
5. Button authorization: only members with `APPROVER_ROLE_ID` may click; other clicks get an ephemeral "not authorized" and the card stays live. All decisions are logged (who, when) into the card thread and returned in `ApprovalOutcome`.
6. Idempotency: a card is bound to the trader's `client_order_id`; double-clicks or Discord retries cannot approve twice (first decision wins, recorded in the card).

### Blocking semantics (the documented exception)
7. `send_trade_card` is awaited by the trader's *entry task only*. The trading cycle (monitoring, exit management, other games' signals) runs in separate tasks and never waits on Discord. If the card can't be delivered (`DiscordUnavailable`), the entry is abandoned (fail-closed) and a retry-queued notify records the miss.

### Reliability
8. All outbound traffic through an async queue with retry/backoff (base 1s, ×2, max 60s). **Overflow policy** (queue > `QUEUE_MAX=200`): drop notify-only messages oldest-first; NEVER drop approval cards, halt acknowledgments, or `critical` notifies — if those can't queue, the system halts new entries (fail-closed) and logs locally.
9. Discord outage in manual-approve mode ⇒ no approvals possible ⇒ no new entries; exits unaffected. On reconnect, expired cards are reconciled (any order still resting past its card's expiry is canceled).

### Slash commands
10. `/positions` — open positions + `ExposureSummary` from risk-management. `/pnl` — daily + lifetime, demo/prod labeled. `/skills` — confirmed skills with win_rate/sample_size (vault query). `/slate` — today's games with match status (incl. `unmatched`/`grammar-unverified` flags). `/halt <reason>` — `risk_management.set_halt(True, reason, caller="discord")`; halt state persists via the ledger note, so a restart stays halted; ack posted. `/resume` — clears halt, requires the same role.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `MODE` | `manual_approve` | Category B: stays until owner answers the execution-mode question |
| `APPROVAL_TIMEOUT_LIVE_S` | 120 | PROPOSED — confirm with risk numbers |
| `APPROVAL_TIMEOUT_PREGAME_S` | 600 | PROPOSED |
| `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` | — | env vars |
| `APPROVER_ROLE_ID` | — | env var; no default, bot refuses to start in manual_approve without it |
| `QUEUE_MAX` | 200 | overflow threshold |

## Edge cases
- **Approve clicked at second N of an N-second timeout:** decision timestamps compared server-side; expiry wins ties (never place after cancel).
- **Card approved but order placement then fails** (`KalshiOrderRejected`): card edits to "rejected by exchange" with the error; no silent retry of a money action.
- **Two cards for the same market** (different skills): allowed to display; the trader's one-position-per-market-per-skill rule governs placement, and card #2 notes the existing position.
- **Bot restarts with cards pending:** on startup, all pending cards from state are expired + reconciled (rule 9); the trader re-proposes if the signal still verifies.
- **Rate limits from Discord:** respected via the queue's backoff; approval cards jump the queue ahead of notifies.
- **`/halt` during an in-flight approved order:** halt blocks new entries; the in-flight order proceeds (it was approved pre-halt) — documented so nobody expects halt to claw back a click.

## Dependencies
risk-management (exposure, halt), vault (skills/slate queries, halt persistence via ledger). Called by: trader (cards), orchestrator + all agents (notify/escalation). Not on the critical path (rule 7's carve-out aside).

## Testing requirements
- Timeout → cancel, never place: clock-controlled test around `APPROVAL_TIMEOUT_LIVE_S`.
- Role gating: unauthorized click no-ops; authorized approve returns outcome once (double-click idempotency).
- Overflow: 201 queued notifies drop oldest; approval card still enqueues.
- Outage: `send_trade_card` during simulated outage → `DiscordUnavailable` → caller abandons entry; reconnect reconciliation cancels a stale resting order.
- Mode flip: autonomous mode posts notify-only card and places without waiting.
- Halt persistence: `/halt` → restart → still halted.

## New types
```python
@dataclass
class TradeCard:
    client_order_id: str; skill_name: str; market: MarketRef
    side: Side; action: Literal["buy", "sell"]
    sizing: SizingResult; snapshot: dict     # entry-condition numbers + timestamps
    is_live: bool                            # selects timeout
@dataclass
class ApprovalOutcome:
    decision: Literal["approved", "rejected", "expired", "undeliverable"]
    decided_by: str | None; decided_at: datetime | None; card_message_id: str | None
```
