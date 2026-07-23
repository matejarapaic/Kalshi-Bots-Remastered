# risk-management

**Trigger:** the trader has a verified entry and needs a position size; a fill/exit/settlement needs recording in the exposure ledger; anyone needs current exposure or halt state.

## What this is for

The single place money math lives. It turns a verified signal into a contract count via Kelly (or flat) sizing, then runs the result through every cap in a fixed order, and it owns the exposure ledger those caps read from. **Every numeric trading parameter in the entire system is named in the table below — no other spec or skill may inline a risk number; they reference these names.** `contracts=0` is a first-class answer, and `capped_by` names every rule that bound.

## Interface

```python
size(req: SizingRequest) -> SizingResult          # never raises for "no"; raises RiskError for infra
on_fill(fill: Fill, market: MarketRef, skill_name: str) -> None
on_exit(fill: Fill, market: MarketRef, skill_name: str) -> None
on_settle(s: Settlement, market: MarketRef, skill_name: str) -> None
exposure() -> ExposureSummary
halted() -> tuple[bool, str | None]               # (halted, reason)
set_halt(on: bool, reason: str, caller: str) -> None
reconcile() -> bool                               # startup: live vs ledger, self-heals settled-while-down
```

Exceptions: `RiskError` (base — infra only: ledger unreadable, balance fetch failed), `RiskUnknownSkill`.

## THE PARAMETER TABLE

### Owner-confirmed 2026-07-17 (from the four confirmed skill notes — restated, not alterable here)
| Name | Value | Meaning |
|---|---|---|
| `BASE_KELLY_FRACTION` | 0.5 | half-Kelly baseline for Kelly-sized skills |
| `MULT_OVERREACTION` | 1.0 | live-win-prob-overreaction |
| `MULT_DIVERGENCE_LIVE` | 1.0 | sportsbook-kalshi-divergence, live |
| `MULT_DIVERGENCE_PREGAME` | 0.5 | same skill, pregame entries |
| `MULT_INJURY` | 0.5 | injury-news-repricing-lag (net quarter-Kelly) |
| garbage-time sizing | FLAT | Kelly forbidden as p→1 (per its note) |
| `CAP_OVERREACTION_PCT` | 5 | per-trade, % of bankroll |
| `CAP_DIVERGENCE_PCT` | 5 | |
| `CAP_INJURY_PCT` | 3 | |
| `CAP_GARBAGE_PCT` | 5 | |
| `CAP_GARBAGE_AGG_PCT` | 10 | simultaneous garbage-time across all games |

### Owner-confirmed 2026-07-17 (Phase 2 checkpoint — "tighter variant" selected)
| Name | Value | Meaning |
|---|---|---|
| `TOTAL_EXPOSURE_CAP_PCT` | 15 | all open positions, % of bankroll |
| `PER_GAME_EXPOSURE_CAP_PCT` | 5 | all skills/markets on one game (one full-size position per game by design) |
| `CORRELATION_SCALE_SAME_GAME` | 0.5 | 2nd+ position on a game is sized after ×0.5; correlated same-team markets count as same-game |
| `DAILY_LOSS_HALT_PCT` | 5 | realized daily loss ≥ this → no new entries until next ET day; exits unaffected |
| `MAX_OPEN_POSITIONS` | 6 | across the system |
| `DEPTH_CONSUMPTION_MAX` | 0.25 | an order may consume ≤25% of book depth within 2¢ of entry |

## Behavior

### Kelly math (written out)
1. For a binary contract at price `c` cents with model win probability `p`: fee-adjusted cost per contract `c_f = c + est_fee_cents(1, c)` (kalshi-client). Edge exists iff `p·100 > c_f`; otherwise return `SizingResult(contracts=0, capped_by=["no_edge"])`.
2. Full-Kelly fraction of bankroll: `f* = (p·100 − c) / (100 − c)` (derived from maximizing log wealth on a `c`-cost, 100-payout binary). Applied fraction: `f = f* × BASE_KELLY_FRACTION × skill multiplier`.
3. **Worked example:** p=0.68, c=60¢, 100 contracts fee context: `est_fee_cents(1,60) = ceil(7·60·40/10000) = 2` → `c_f=62 < 68`, edge exists. `f* = (68−60)/(100−60) = 0.20`; overreaction live: `f = 0.20 × 0.5 × 1.0 = 0.10`; per-trade cap 5% binds (0.05 < 0.10) → capped_by gains `"per_trade_cap"`. Bankroll $500.00 (50,000¢): budget = 2,500¢; contracts = `floor(2500 / 62) = 40`; final fee re-estimated on 40 contracts: `est_fee_cents(40,60) = ceil(7·40·60·40/10000) = 68¢` → `SizingResult(contracts=40, limit_price=60, kelly_fraction_used=0.10→capped 0.05, capped_by=["per_trade_cap"], est_fee_cents_total=68)`.
4. Flat sizing (garbage-time only): budget = `CAP_GARBAGE_PCT`% of bankroll; contracts = `floor(budget / c_f)`; `kelly_fraction_used=None`.

### Order of operations (fixed; each binding step appends to `capped_by`)
5. (1) fee-adjusted edge check → (2) raw Kelly/flat fraction → (3) skill multiplier (`RiskUnknownSkill` if the skill name isn't in the table — **never a default multiplier**) → (4) per-trade cap → (5) correlation scaling: if the ledger holds any open position on the same `espn_event_id` (or a market on the same team-pair), multiply the surviving fraction by `CORRELATION_SCALE_SAME_GAME` → (6) per-game cap: new + existing exposure on this game ≤ `PER_GAME_EXPOSURE_CAP_PCT` → (7) skill-aggregate caps (currently only `CAP_GARBAGE_AGG_PCT`) → (8) total exposure cap → (9) daily-loss halt check (`capped_by=["daily_loss_halt"]`, contracts=0) → (10) max-open-positions check → (11) **depth gate**: contracts ≤ `DEPTH_CONSUMPTION_MAX × req.book_depth_at_entry`; also enforce the skill note's absolute depth minimum (200/200/100/300 per the four notes — read from a per-skill table mirroring the notes) → (12) floor to integer; `contracts < 1` → 0 with the last binding cap named.
6. Exposure accounting basis: a position's exposure = entry cost + fees (cents actually at risk), computed from ledger fills, marked against bankroll = `get_balance()` + open exposure (so caps don't loosen as cash converts to positions).

### The ledger
7. Owns open positions (by market_ticker: skill, side, contracts, cost, fees, espn_event_id, opened_at), realized P&L by ET day, and halt state. Updated only via `on_fill`/`on_exit`/`on_settle`. Persisted via the **vault skill** to `03-market-context/exposure-ledger.md` (frontmatter = machine state, body = human-readable table; write-through on every mutation, reload on startup — restart-safe). *(Location is a Category A choice: it's live-cycle state, so it lives in 03-market-context.)*
8. Sizing requests are serialized through a single lock with ledger mutations — two concurrent `size()` calls cannot both pass the same headroom check.
9. `set_halt` callers: discord-bot (`/halt`, `/resume`), daily-loss rule (automatic), orchestrator (hard-rule violations). Halt state persists in the ledger note (restart keeps a halted system halted).

## Configuration
The parameter table above **is** the configuration; implemented as one Python module (`risk_params.py`) of named constants with statuses (`CONFIRMED`/`PROPOSED`) and provenance comments. Changing any `CONFIRMED` value requires owner sign-off by build rule; the module docstring says so.

## Edge cases
- **Balance fetch fails:** `RiskError`; trader treats as no-entries cycle (its prompt's error rule).
- **Unknown skill name:** `RiskUnknownSkill` — a misconfigured skill must never size with borrowed numbers.
- **p ≤ fee-adjusted breakeven:** `capped_by=["no_edge"]`, contracts 0 (rule 1).
- **Depth thinner than one contract at the cap:** contracts 0, `capped_by=["depth_gate"]`.
- **Ledger/live divergence:** detected on startup via `reconcile()` (`get_positions()` vs the ledger). A ledger position missing from live is checked against `get_settlements(ticker)` first — settled-while-down is the expected restart case, self-healed here (position popped, P&L booked from the ledger's own cost basis and the settlement's win/loss result, `daily_pnl` updated, `last_reconcile_settled[ticker]` exposed for the trader to close the matching trade note) rather than halted. Only an unexplained difference — a live position the ledger doesn't know about ("ghost"), or a missing ledger position with no settlement record yet — halts + alerts (never trade on a ledger known to be wrong). Deliberately does **not** trust the settlement's `revenue_cents` for P&L, since that field reflects the whole account's history on that market, not just this ledger's position. A halt `reconcile()` itself set auto-clears the moment a later call finds everything explained (e.g. the settlement record shows up a minute after the first restart's reconcile ran) — a manual halt (`caller="discord"`) never auto-clears; only a human `/resume` lifts one of those.
- **Partial fills:** `on_fill` records actual filled contracts; unfilled remainder releases its exposure reservation when the order is canceled/expires (trader manages the order; ledger reserves on `size()`? — no: reservation happens at `on_fill` only; the serialization lock plus immediate fill-or-cancel trading style keeps the race window acceptable; documented limitation).
- **Same-team correlated markets** (e.g. a future series/championship market on a team currently playing): counts as same-game for correlation scaling; detection via team pair, not just event id.
- **ET day rollover mid-session:** daily P&L buckets keyed by ET calendar date; a halt from yesterday clears at rollover automatically (reason logged).

## Dependencies
kalshi-client (balance, fee estimates, positions for reconcile), vault (ledger persistence). Called by: trader (sizing, fills), discord-bot (halt/exposure queries), postmortem (reads ledger history via vault).

## Testing requirements
- Kelly: the worked example above asserted exactly; `f*` formula boundary cases (p=c/100 → no edge; p=1 → f*=1 capped); negative-edge rejection incl. fee-push (p>c but p<c_f).
- Flat sizing: garbage-time example with fee-adjusted cost; aggregate cap across 3 simulated concurrent games binding at the 3rd.
- Order-of-operations: fixtures where each cap (4)–(11) is the binding one, asserting `capped_by` contents and order.
- Correlation: second position on same event scaled ×0.5; same-team different-market counted.
- Daily-loss halt: losses crossing the threshold mid-day block entry #N+1; ET rollover unblocks; restart persistence.
- Ledger: fill→exit→settle lifecycle; startup reconcile mismatch → halt; reconcile self-heals a ledger position that settled while down (P&L booked, position popped, no halt) vs. still halting on a live position the ledger can't explain.
- Serialization: two concurrent size() calls against headroom for one — exactly one sized.

## New types
```python
@dataclass
class ExposureSummary:
    bankroll_cents: int; open_cost_cents: int
    by_game: dict[str, int]; by_skill: dict[str, int]
    open_positions: int; daily_realized_pnl_cents: int
    halted: bool; halt_reason: str | None
```
