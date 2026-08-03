# risk-management

**Trigger:** the trader has a verified entry and needs a position size; a fill/exit/settlement needs recording in the exposure ledger; anyone needs current exposure or halt state.

## What this is for

The single place money math lives. It turns a verified signal into a contract count via Kelly (or flat) sizing, then runs the result through every cap in a fixed order, and it owns the exposure ledger those caps read from. **Every numeric trading parameter in the entire system is named in the table below — no other spec or skill may inline a risk number; they reference these names.** `contracts=0` is a first-class answer, and `capped_by` names every rule that bound.

## Interface

```python
size(req: SizingRequest) -> SizingResult          # never raises for "no"; raises RiskError for infra
cancel_intent(market_ticker: str, skill: str) -> None  # release a sizing reservation (order canceled/rejected/expired)
on_fill(fill: Fill, market: MarketRef, skill_name: str, event_id: str = "") -> None
on_exit(fill: Fill, market: MarketRef, skill_name: str) -> int | None  # returns the fee-inclusive realized pnl for this fill, or None if no matching open position
on_settle(s: Settlement, market: MarketRef, skill_name: str) -> None
exposure() -> ExposureSummary
halted() -> tuple[bool, str | None]               # (halted, reason)
set_halt(on: bool, reason: str, caller: str) -> None
reconcile() -> bool                               # startup: live vs ledger, self-heals settled-while-down
kelly_fraction(p: Prob, c: Cents) -> float        # module-level: full-Kelly f* for a binary at c cents
required_edge_cents(entry_price_cents: int) -> int  # module-level: fee-aware entry-edge floor
```

Exceptions: `RiskError` (base — infra only: balance fetch failed), `RiskUnknownSkill`. An unreadable or absent ledger note at startup is a fresh ledger, not an error.

## THE PARAMETER TABLE

Implemented as module-level constants in `kalshi_bots/skills/risk_management.py`, each with a provenance comment.

### CONFIRMED 2026-07-17 (category-agnostic; carried unchanged through the crypto pivot)
| Name | Value | Meaning |
|---|---|---|
| `BASE_KELLY_FRACTION` | 0.5 | half-Kelly baseline for Kelly-sized skills |

### PROPOSED 2026-07-22 (crypto pivot defaults, pending owner confirmation; skills are draft-status until postmortems accumulate 30+ settled samples)
| Name | Value | Meaning |
|---|---|---|
| `SKILL_RISK_MULTIPLIER["btc-15min-fair-value"]` | (1.0, 1.0) | (live, non_live) columns; crypto trades 24/7 so only live applies today |
| `SKILL_RISK_MULTIPLIER["btc-15min-orderflow-imbalance"]` | (1.0, 1.0) | |
| `SKILL_RISK_MULTIPLIER["btc-15min-vol-spike"]` | (0.5, 0.5) | net quarter-Kelly |
| `PER_TRADE_CAP_PCT["btc-15min-fair-value"]` | 5 | per-trade, % of bankroll |
| `PER_TRADE_CAP_PCT["btc-15min-orderflow-imbalance"]` | 5 | |
| `PER_TRADE_CAP_PCT["btc-15min-vol-spike"]` | 3 | |
| `SKILL_MIN_DEPTH` (all three skills) | 100 | absolute depth minimum: contracts within 2¢ of entry, per skill note |
| `MAX_CONTRACTS_PER_WINDOW` | 20 | hard contract cap per 15-min window — draft-skill training wheels |
| `MIN_EDGE_CENTS` | 4 | fair-value entry: flat model-vs-ask divergence floor. The monitor reads it directly to flag candidates; the trader enters against the fee-aware `required_edge_cents(price)` (≥ this floor) instead — see the 2026-07-24 P1 block below |
| `EXIT_EDGE_CENTS` | 1 | fair-value exit: held-side edge at/below this = thesis played out |
| `SIGMA_PLAUSIBLE_MIN/MAX` | 0.18 / 2.00 | annualized vol band outside which the model is not trusted (MIN lowered from 0.20 2026-08-02, owner override — the original 0.20 was an unfit illustrative example from the pivot brief; median observed sigma across documented windows is 0.187) |
| `MIN_DEPTH_WITHIN_5C` | 100 | entry gate: contracts within 5¢, each side |
| `DEPTH_COLLAPSE_FRACTION` | 0.5 | exit when either side < `MIN_DEPTH_WITHIN_5C ×` this |
| `ENTRY_PHASES` | `("midpoint",)` | no entries in opening or near_close |

### PROPOSED 2026-07-24 (universal hard risk backstop, independent of any skill's own thesis-invalidation exits; awaiting owner sign-off)
| Name | Value | Meaning |
|---|---|---|
| `STOP_LOSS_PCT` | 50 | exit any open position, any skill, once its current mark-to-market value has fallen to this fraction (%) of entry cost — checked in `Trader._exit_reason` before per-skill rules |

### PROPOSED 2026-07-24 (P1 entry-quality gates from the first live-session postmortem — its losers clustered on near-ATM coin-flips whose thin edge did not survive round-trip taker fees; awaiting owner sign-off — validate over the next live sample before promoting)
| Name | Value | Meaning |
|---|---|---|
| `ATM_MIN_SIGMA_DISTANCE` | 0.5 | fair-value entry: decline when `fair_value_model.moneyness_sigmas` < this — the strike is within half a settlement-distribution stddev of spot (a coin flip). Surfaces as the `not_at_the_money` entry condition |
| `ENTRY_EDGE_SLIPPAGE_CENTS` | 1 | buffer added on top of round-trip taker fees by `required_edge_cents` |

`required_edge_cents(entry_price_cents) -> int` returns the fee-aware entry-edge floor: `max(MIN_EDGE_CENTS, 2·est_fee_cents(1, price) + ENTRY_EDGE_SLIPPAGE_CENTS)`. The trader uses it in place of the flat `MIN_EDGE_CENTS`, so the modeled edge must clear round-trip taker fees (which peak at the money) plus slippage, not merely 4¢.

### PROPOSED 2026-07-22 (values carried unchanged from the owner-confirmed 2026-07-17 Phase-2 "tighter variant" checkpoint; re-scoped per-event for crypto windows, pending owner re-confirmation for this market family)
| Name | Value | Meaning |
|---|---|---|
| `TOTAL_EXPOSURE_CAP_PCT` | 15 | all open positions, % of bankroll |
| `PER_EVENT_EXPOSURE_CAP_PCT` | 5 | all skills/markets on one event (one full-size position per window by design) |
| `CORRELATION_SCALE_SAME_EVENT` | 0.5 | 2nd+ position on an event is sized after ×0.5; correlation labels are market tickers |
| `MAX_OPEN_POSITIONS` | 6 | across the system, counting unexpired sizing intents |
| `DEPTH_CONSUMPTION_MAX` | 0.25 | an order may consume ≤25% of book depth within 2¢ of entry |

### Operational constants (not risk numbers)
| Name | Value | Meaning |
|---|---|---|
| `INTENT_TTL_S` | 180 | sizing-intent reservation window (see rule 7) |
| `LEDGER_PATH` | `03-market-context/exposure-ledger.md` | ledger note path, via the vault skill |

## Live overrides (tuner layer)

### PROPOSED 2026-07-28 (live-tuning corridors, awaiting owner sign-off)

The tuner skill may adjust parameters at runtime inside a per-parameter corridor. The **tighten** direction is still hard-bounded at the configured multiplier — live tuning can always make the system more conservative than the owner signed off on. The **relax** direction is *not* capped at baseline (owner-directed 2026-07-30, reversing the original "ceiling is the human-approved baseline" design): it floors at 0 for quantities that can't go negative (edge/depth-type params) and is otherwise uncapped, so a sustained winning or no-trade streak can now push a parameter looser than the owner-confirmed baseline. See `skills/tuner/SKILL.md` for the streak policy that drives this and `CLAUDE.md`'s prod/live gates section for the sign-off. The module constants above remain the single source of baselines; overrides are a layer on top, held in module state (`_overrides`) with a bounded audit log (`override_log`, deque maxlen 100). Overrides are session-scoped (owner-directed 2026-08-02): they die with the process and are never re-applied across a restart — the tuner discards its persisted copy at startup (`Tuner.reset()`), so every session begins at the human-approved baselines.

```python
current(name: str, skill: str | None = None)      # effective value: override if set, else baseline — THE read path for every tunable
baseline(name: str, skill: str | None = None)     # the live module constant (monkeypatch-friendly: never a frozen copy)
set_override(name, value, *, skill=None, reason, caller) -> (old, new)   # corridor-validated; raises RiskOverrideError, applies nothing, on violation
clear_override(name, skill=None) -> bool           # fully re-track the baseline
clear_all_overrides() -> None
has_override(name, skill=None) -> bool
active_overrides() -> dict                         # serializable snapshot: "NAME" / "NAME|skill" -> value
```

`TUNABLE_BOUNDS` maps each tunable name to `(floor_mult, ceil_mult)`, but only the *tighten*-direction multiplier is still an enforced bound: `ceil 1.0` entries tighten by lowering (sizing/exposure/velocity params, floors 0.25×–0.5×) and may now relax upward with no ceiling; `(1.0, 2.0)` entries tighten by *raising* (`MIN_EDGE_CENTS`, `EXIT_EDGE_CENTS`, depth minimums, `SIGMA_PLAUSIBLE_MIN`) and may now relax down to 0. Integer parameters floor at 1 on the tighten side only (count caps can shrink but never hit zero); integer overrides are stored as ints. Per-skill dict parameters (`SKILL_RISK_MULTIPLIER`, `PER_TRADE_CAP_PCT`, `SKILL_MIN_DEPTH`) take a `skill=` key; `SKILL_RISK_MULTIPLIER` overrides are (live, non_live) tuples validated element-wise. `ENTRY_PHASES` is non-numeric and deliberately not tunable. `RiskOverrideError(RiskError)` is the typed rejection.

**Read-path consequence:** `RiskManager._size_locked` reads every tunable via `current()`, and the trader and window-monitor agents now read `MIN_EDGE_CENTS`, `EXIT_EDGE_CENTS`, `STOP_LOSS_PCT`, `SIGMA_PLAUSIBLE_*`, `MIN_DEPTH_WITHIN_5C`, `DEPTH_COLLAPSE_FRACTION` via `current()` at decision time instead of freezing them at import — a live override reaches every consumer on its next evaluation. Non-tunable reads (skill-membership checks, `INTENT_TTL_S`, `ENTRY_PHASES`) stay direct.

Policy (when/what/how far per step) lives in the tuner skill's spec; this layer owns only the mechanism and the corridor enforcement.

## Behavior

### Kelly math (written out)
1. For a binary contract at price `c` cents with model win probability `p`: fee-adjusted cost per contract `c_f = c + est_fee_cents(1, c)` (kalshi-client). Edge exists iff `p·100 > c_f`; otherwise return `SizingResult(contracts=0, capped_by=["no_edge"])`.
2. Full-Kelly fraction of bankroll: `f* = (p·100 − c) / (100 − c)` (module-level `kelly_fraction`, derived from maximizing log wealth on a `c`-cost, 100-payout binary). Applied fraction: `f = f* × BASE_KELLY_FRACTION × skill multiplier`, using the (live, non_live) column selected by `req.is_live` — the columns are currently equal for every crypto skill; the mechanism is kept. `kelly_fraction_used` reports this applied fraction *before* caps (caps report through `capped_by`, never by mutating it).
3. **Worked example** (btc-15min-fair-value, live): p=0.68, c=60¢, book depth 1,000, bankroll $500.00 (50,000¢), empty ledger. `est_fee_cents(1,60) = ceil(7·60·40/10000) = 2` → `c_f=62 < 68`, edge exists. `f* = (68−60)/(100−60) = 0.20`; `f = 0.20 × 0.5 × 1.0 = 0.10`; per-trade cap 5% binds (0.05 < 0.10) → capped_by gains `"per_trade_cap"`. Budget = 2,500¢; `floor(2500 / 62) = 40` contracts; depth gate allows 250, no bind; the per-window contract cap binds (40 > 20) → 20 contracts, capped_by gains `"per_window_contract_cap"`. Final fee re-estimated on 20 contracts: `est_fee_cents(20,60) = ceil(7·20·60·40/10000) = 34¢` → `SizingResult(contracts=20, limit_price=60, kelly_fraction_used=0.10, capped_by=["per_trade_cap","per_window_contract_cap"], est_fee_cents_total=34)`, and a 1,234¢ sizing intent (cost + fees) is reserved for 180 s.
4. Flat sizing (skill multiplier `None` — for any skill whose note forbids Kelly): budget = the skill's `PER_TRADE_CAP_PCT`% of bankroll; contracts = `floor(budget / c_f)`; `kelly_fraction_used=None`. No crypto skill currently flat-sizes; the mechanism is kept and tested for future skill notes that require it.

### Order of operations (fixed; each binding step appends to `capped_by`)
5. A skill name absent from the parameter table raises `RiskUnknownSkill` before any step runs — **never a default multiplier**. Then: (1) fee-adjusted edge check → (2) raw Kelly/flat fraction → (3) skill multiplier → (4) per-trade cap (Kelly-sized skills only; flat sizing already *is* the cap) → (5) velocity scaling: if `req.recent_move_pct` is not `None` and its magnitude ≥ `VELOCITY_THRESHOLD_PCT`, multiply the surviving fraction by `VELOCITY_SIZE_SCALE` (`"velocity_scale"`) — direction-agnostic (a fast move either way scales the same) and applies to every skill, since entries only ever happen at `midpoint` anyway → (6) correlation scaling: if positions or unexpired intents already commit exposure to the same event — matched by event id (`req.event_id`, falling back to the signal window's event ticker) *or* overlapping correlation labels (market tickers) — multiply the surviving fraction by `CORRELATION_SCALE_SAME_EVENT` (`"correlation_same_event"`) → (7) per-event cap: budget clamped so new + existing commitment on this event ≤ `PER_EVENT_EXPOSURE_CAP_PCT` (`"per_event_cap"`) → (8) total exposure cap: budget clamped to `TOTAL_EXPOSURE_CAP_PCT` minus committed cost (`"total_exposure_cap"`) → (9) halt gate: a manual/reconcile halt → contracts 0, `"halted"` (the automatic daily-loss halt was removed 2026-08-02, owner-directed — realized daily losses no longer gate sizing) → (10) max-open-positions: open positions + unexpired intents ≥ `MAX_OPEN_POSITIONS` → contracts 0, `"max_open_positions"` → (11) depth gate: depth below the skill's `SKILL_MIN_DEPTH` → contracts 0, `"depth_min"`; else contracts = `budget // c_f`, clamped to `DEPTH_CONSUMPTION_MAX × req.book_depth_at_entry` (`"depth_gate"`) → (12) per-window contract cap: clamped to `MAX_CONTRACTS_PER_WINDOW` (`"per_window_contract_cap"`) → (13) integer floor; `contracts < 1` → 0 with the last binding cap named (`"no_room"` if none bound).
6. Exposure accounting basis: a position's exposure = entry cost + fees (cents actually at risk), computed from ledger fills, marked against bankroll = `get_balance()` + open *position* cost (so caps don't loosen as cash converts to positions; intents are excluded from bankroll because their cash is still in the balance). Cap headroom (correlation, per-event, total, max-open) counts positions **plus unexpired sizing intents** — a reserved slot is a consumed slot.

### Sizing intents
7. A successful `size()` reserves an intent keyed `market_ticker|skill` carrying the full estimated cost (contracts × price + fees), event id, and correlation labels, expiring after `INTENT_TTL_S`. It is released by `on_fill` for the same market/skill, by `cancel_intent` (trader calls it when the order is canceled/rejected/expires), or by TTL expiry. Documented limitation: an order still unfilled past 180 s frees headroom before its cancel lands; the serialization lock plus the fill-or-cancel trading style keeps the race window acceptable.

### The ledger
8. Owns open positions (by market_ticker: skill, side, contracts, cost incl. fees, event_id, correlation labels, opened_at), realized P&L by ET day, and halt state. Updated only via `on_fill`/`on_exit`/`on_settle`. Persisted via the **vault skill** to `03-market-context/exposure-ledger.md` (frontmatter = machine state, body = human-readable table; write-through on every mutation, reload on startup — restart-safe). *(Location is a Category A choice: it's live-cycle state, so it lives in 03-market-context.)*
9. `on_fill` accumulates (repeated partial fills add contracts and cost) and pops the matching intent. `on_exit` supports partial exits: pro-rata cost basis released; realized P&L = (portion × price − taker fee) − basis, booked to the current ET day; position popped at zero contracts; an exit fill for a market the ledger doesn't hold is a no-op. An exit fill labeled with the *opposite* side of the held position (Kalshi labels the fill that closes a NO position as a YES buy, priced in yes-cents — verified live 2026-07-29) has its price flipped (`100 − price`) into the position's own terms before netting against the basis. `on_settle` pops the position and books `revenue_cents − cost` (an in-session settlement of the ledger's own position — contrast `reconcile()`, which never trusts `revenue_cents`).
10. Sizing requests are serialized through a single lock with ledger mutations — two concurrent `size()` calls cannot both pass the same headroom check.
11. `set_halt` callers: discord-bot (`/halt`, `/resume`), orchestrator (hard-rule violations), `reconcile()`. The stored reason is `"{reason} (by {caller})"`. Halt state persists in the ledger note (restart keeps a halted system halted). Halts and caps gate `size()` only — exits and settlements are always recordable (exits are never approval-gated anywhere in the system).

## Configuration
The parameter table above **is** the configuration; one module of named constants with statuses (`CONFIRMED`/`PROPOSED`) and provenance comments. Changing any `CONFIRMED` value requires owner sign-off by build rule; the module docstring says so. The live-override layer does not weaken this: baselines still change only by editing the module with sign-off; runtime overrides are corridor-bound at-or-tighter-than the baseline and are audit-logged.

## Edge cases
- **Balance fetch fails:** `size()` raises `RiskError`; trader treats as a no-entries cycle (its prompt's error rule). `exposure()` degrades instead (balance treated as 0) so status queries still answer.
- **Unknown skill name:** `RiskUnknownSkill` — a misconfigured skill must never size with borrowed numbers.
- **p ≤ fee-adjusted breakeven:** `capped_by=["no_edge"]`, contracts 0 (rule 1).
- **Depth below the skill minimum:** contracts 0, `capped_by=[..., "depth_min"]`; depth thinner than one contract at the consumption cap zeroes out via the integer floor with `"depth_gate"` named.
- **Ledger/live divergence:** detected on startup via `reconcile()` (`get_positions()` vs the ledger). A ledger position missing from live is checked against `get_settlements(ticker)` first — settled-while-down is the expected restart case, self-healed here (position popped, P&L booked from the ledger's own cost basis and the settlement's win/loss result — win iff `result == side`: `contracts×100 − cost`; loss: `−cost` — `daily_pnl` updated, `last_reconcile_settled[ticker]` exposed for the trader to close the matching trade note) rather than halted. Only an unexplained difference — a live position the ledger doesn't know about ("ghost"), a missing ledger position with no settlement record yet, or a settlement fetch that itself fails — halts + returns False (never trade on a ledger known to be wrong). Deliberately does **not** trust the settlement's `revenue_cents` for P&L, since that field reflects the whole account's history on that market, not just this ledger's position. A halt `reconcile()` itself set auto-clears the moment a later call finds everything explained (e.g. the settlement record shows up a minute after the first restart's reconcile ran) — a manual halt (`caller="discord"`) never auto-clears; only a human `/resume` lifts one of those.
- **Two skills, one market:** correlation matching runs on event id *or* label overlap, so a second skill entering the same market ticker scales ×0.5 even when the two requests' event-id strings differ.
- **ET day rollover mid-session:** daily P&L buckets are still keyed by ET calendar date (reporting/postmortems only — since the 2026-08-02 owner-directed removal of the daily-loss halt, the buckets no longer gate sizing). A `set_halt` halt survives rollover.

## Dependencies
kalshi-client (balance, `est_fee_cents`, positions + settlements for reconcile), vault (ledger persistence). Called by: trader (sizing, intents, fills/exits/settlements), discord-bot (halt/exposure queries), orchestrator (`reconcile()` at startup, hard-rule halts), postmortem (reads ledger history via vault).

## Testing requirements
(`tests/test_risk_management.py`, grouped as below)
- `TestKellyMath`: the worked example asserted exactly with the per-window contract cap lifted (40 contracts, `capped_by=["per_trade_cap"]`, fee 68¢ — the cap-lifted variant isolates the % pipeline); no edge at `p = c/100`; fee-push rejection (p=0.61, c=60 → c_f=62 > 61); vol-spike quarter-Kelly hitting its 3% cap; the (live, non_live) multiplier mechanism via an injected table entry; unknown skill raises; balance failure raises.
- `TestFlatSizing`: multiplier-`None` mechanism via injection (`kelly_fraction_used=None`, budget = per-trade cap %) — kept green though no crypto skill flat-sizes yet.
- `TestCaps`: fixtures where each cap is the binding one — per-window contract cap on the default path (40 → 20); correlation on a shared event id and on a shared market ticker with differing event ids; per-event cap; total exposure cap; max-open-positions counting intents; depth minimum; depth consumption gate — asserting `capped_by` contents and order.
- `TestHalts`: manual halt blocks sizing; daily realized losses of any size leave sizing unaffected (pins the 2026-08-02 owner-directed removal of the daily-loss halt); halt persists across restart (ledger reload).
- `TestLedgerLifecycle`: fill→partial-exit→settle lifecycle with pro-rata basis; settlement-win P&L; reconcile mismatch → halt; reconcile self-heals settled-while-down wins *and* losses (P&L booked, position popped, no halt); halt on an unexplained missing position; a reconcile-set halt auto-clears once resolved; a manual halt never does.
- `TestIntentSerialization`: two `size()` calls against headroom for one — the second is capped by the first's intent; `cancel_intent` releases the reservation.
- `TestOverrides`: `current()` falls back to the (monkeypatchable) baseline; a set override changes the next `size()` result; corridor violations raise `RiskOverrideError` and apply nothing (both directions, incl. above-baseline on lower-is-tighter params); non-tunable names rejected; per-skill tuple validation; `clear_override` re-tracks the baseline; `active_overrides` round-trips through `set_override`.

## New types
```python
@dataclass
class ExposureSummary:
    bankroll_cents: int; open_cost_cents: int
    by_event: dict[str, int]; by_skill: dict[str, int]
    open_positions: int; daily_realized_pnl_cents: int
    halted: bool; halt_reason: str | None
```
