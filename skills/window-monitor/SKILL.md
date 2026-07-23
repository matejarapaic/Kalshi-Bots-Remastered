# window-monitor

**Trigger:** any component needs to know which 15-minute contract is live right now, what lifecycle phase it is in, or its strike.

`grammar_verified: 2026-07-22` (ticker grammar derived from live prod + demo `KXBTC15M` markets, cross-checked against `close_time` and `rules_primary`)

## What this is for

Entity resolution for "what should I be trading right now?" — the architectural role the deleted league-matching skill played, translated to a clock-driven market family. 15-minute windows tile the clock 24/7, so the *candidate* window is pure time math; whether it is *tradeable* is an API question. The hard invariant carries over unchanged: **a window that cannot be verified against the live API is `None`, never a guess.**

## Ticker grammar (verified live)

```
event ticker:  {SERIES}-{YY}{MON}{DD}{HHMM}     e.g. KXBTC15M-26JUL222130
market ticker: {event}-{MM}                     e.g. KXBTC15M-26JUL222130-30
```

- `YY MON DD HHMM` encode the window **close** time in **US Eastern wall clock** (EDT or EST as in effect that day — always derived via `America/New_York`, never a fixed UTC offset). `MON` is an uppercase 3-letter month; `MM` duplicates the close minute (`00|15|30|45`).
- Live cross-check: `KXBTC15M-26JUL222130-30` closes 2026-07-23 01:30Z = 9:30 PM EDT Jul 22; `rules_primary` names "9:30 PM EDT on Jul 22, 2026" verbatim.
- One **binary** market per window ("BTC price up in next 15 mins?", `strike_type: greater_or_equal`) — *not* a strike ladder. `floor_strike` is stamped at window open with the prior window's 60s-BRTI settlement average (`expiration_value` chains window to window). Ties settle YES.
- Trading window: `open_time = close_time − 15min` on prod (demo pre-opens markets ~a day early — phase math keys off the settlement timeline, never `open_time`).

## Interface

```python
# pure (no I/O)
def next_quarter_close(now: datetime) -> datetime
def event_ticker_for_close(close_utc: datetime, series="KXBTC15M") -> str
def market_ticker_for_close(close_utc: datetime, series="KXBTC15M") -> str
def parse_market_ticker(ticker: str) -> tuple[str, datetime]   # raises TickerGrammarError
def active_window(now: datetime, series="KXBTC15M") -> WindowRef   # strike=None always
def window_phase(now: datetime, w: WindowRef) -> Phase

# API-verified (owns a kalshi-client reference)
class WindowResolver:
    def resolve_active(self, now: datetime) -> WindowRef | None
    def strike_for_window(self, w: WindowRef) -> float | None
```

Exceptions: `WindowMonitorError` (base; also raised on naive datetimes), `TickerGrammarError`.

## Behavior

1. **Window boundaries:** the window containing `now` closes at the next quarter-hour boundary *strictly after* `now` — a boundary instant belongs to the window that just opened, not the one that just settled.
2. **Phases** (config below): `opening` `[open, open+OPENING_PHASE_S)`, `midpoint` until `close − NEAR_CLOSE_PHASE_S`, `near_close` until close, `settled` at/after close. Times before open clamp to `opening`.
3. **Verification before trading** (`resolve_active`): construct the expected ticker from the clock, fetch the market via `kalshi-client.get_market_raw`, and require (a) the API `close_time` equals the constructed close exactly, (b) `status ∈ {active, open}`. Any failure → `None`. This is the never-guess rule: grammar drift, API outage, and the DST fold all fail closed.
4. **Strike:** `floor_strike` may be absent for the first moments after open; `resolve_active` returns the window with `strike=None` and re-fetches until it appears. Callers that need the strike (fair-value) must gate on it.
5. **Caching:** verification results cache per market ticker, bounded at `VERIFY_CACHE_MAX` (insertion-pruned). Negative results expire after `NEGATIVE_TTL_S` — a market flips `initialized→active` right at window open, and a permanent negative would blind the whole window.
6. **DST fold (UNVERIFIED live):** when DST ends (e.g. 2026-11-01), the 1:00–2:00 AM ET wall clock repeats and two distinct UTC windows construct the *same* ticker. How Kalshi disambiguates is unverified (unobservable in July). `parse_market_ticker` resolves to the first occurrence (fold=0); rule 3's close_time check is the authority — on the ambiguous hour the resolver declines rather than mislabeling. Re-verify against live markets during the Nov 1 fold and update this section.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `DEFAULT_SERIES` | `KXBTC15M` | nothing else hard-codes BTC |
| `WINDOW_S` | 900 | contract length |
| `OPENING_PHASE_S` | 120 | PROPOSED 2026-07-22: strike/book still settling; no entries |
| `NEAR_CLOSE_PHASE_S` | 180 | PROPOSED 2026-07-22: gamma zone; exits only |
| `TRADEABLE_MARKET_STATUSES` | `{active, open}` | |
| `VERIFY_CACHE_MAX` | 16 | ~4 windows/hour; memory-bounded |
| `NEGATIVE_TTL_S` | 10s | retry cadence for failed verification |

## Edge cases
- **ET-midnight rollover:** a window closing 00:00 ET is labeled with the *new* day (live-verified: `KXBTC15M-26JUL240000-00` closes 04:00Z Jul 24 = midnight EDT Jul 24). Falls out of tz conversion; test pinned.
- **EST vs EDT:** January windows encode EST wall clock (verified by construction, `test_est_window_uses_wall_clock_not_fixed_offset`); a hardcoded −4 offset would mislabel every winter ticker by an hour.
- **`status=open` REST filter is unreliable** (observed returning closed markets); select the live market by constructed ticker or `min_close_ts`/`max_close_ts`, never by status filter.
- **Grammar drift:** if Kalshi changes the format, rule 3 makes every window resolve `None` (loud, safe); fix the grammar, re-stamp `grammar_verified`.

## Dependencies
`kalshi-client` (`get_market_raw`). Consumed by: the window-monitor agent (`kalshi_bots/agents/window_monitor.py`), trader (phase-based exits), fair-value-model (time remaining, strike).

## Testing requirements
- Grammar fixtures are the live-captured tickers of 2026-07-22 (including the ET-midnight rollover pair), not synthetic examples.
- Round-trip parse↔construct; EST (January) construction; DST-end fold collision documented and pinned; malformed tickers raise.
- Boundary-instant window assignment; full phase timeline across a synthetic window including exact boundaries.
- Resolution: verified-with-strike; missing market → None; close_time mismatch → None; non-tradeable status → None; positive cache hit; strike re-fetch; negative-cache TTL expiry; cache bound.

## New types
`WindowRef`, `Phase` (CONTRACTS.md, sprint-2).
