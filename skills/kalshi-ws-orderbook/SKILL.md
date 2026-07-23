# kalshi-ws-orderbook

**Trigger:** any component needs a live order book for the active contract, or the BRTI settlement index stream.

`api_verified: 2026-07-22` (docs.kalshi.com asyncapi.yaml "Kalshi Market Data WebSocket API v2.0.0" + live endpoint probes: both WS hosts alive and enforcing auth. **Live subscribe/message flow NOT yet verified end-to-end**: the demo API key in `.env` is dead — both REST and WS return `authentication_error NOT_FOUND`, so run `scripts/smoke_kalshi_ws.py` and re-stamp this line once the owner issues a fresh demo key. Message shapes below are from the official asyncapi spec, and the BRTI value-field parse is deliberately defensive until a live frame confirms it.)

## What this is for

Streaming order books over Kalshi's market-data WebSocket, replacing the prior build's per-cycle REST orderbook polls. Also owns the `cfbenchmarks_value` channel: Kalshi streams **BRTI itself** — the index these contracts settle on — including `last_60s_windowed_average_15min`, the settlement average as it forms during each window's final minute. For settlement-referenced trading that stream is ground truth; the crypto-price-feed composite is the independent cross-check.

## Wire facts (verified)

- **Endpoints:** prod `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, demo `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` (legacy hosts remain supported; overridable via `KALSHI_WS_HOST_PROD`/`_DEMO`).
- **Auth is required for the connection itself** — there is no public WS channel. Same RSA-PSS scheme as REST, signed over `timestamp + "GET" + "/trade-api/ws/v2"`, applied as connect headers via kalshi-client's `ws_auth_headers()`.
- **Subscribe shapes:** `{"id":N,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_ticker":T}}`; BRTI: `{"id":N,"cmd":"subscribe","params":{"channels":["cfbenchmarks_value"],"index_ids":["BRTI"]}}`; `unsubscribe` takes `sids`; `update_subscription` supports `action: get_snapshot`.
- **Data shapes:** one `orderbook_snapshot` then incremental `orderbook_delta`s. Prices are dollar strings (`price_dollars`, `yes_dollars_fp`/`no_dollars_fp` pairs) with sub-cent ticks in the tails; sizes are fixed-point strings and may be **fractional**. `seq` must be contiguous per subscription. Books are one-sided bids per side, same as REST. Server pings every 10s (the websockets library auto-pongs).

## Interface

```python
class KalshiOrderBook:
    async def start(self) -> None            # connect loop (reconnect + resubscribe)
    async def stop(self) -> None
    async def subscribe(self, market: MarketRef) -> None
    async def unsubscribe(self, market_ticker: str) -> None
    def snapshot(self, market_ticker: str) -> OrderbookSnapshot | None  # thread-safe
    def health(self, market_ticker: str) -> BookHealth
    def brti(self) -> BrtiState | None
```

Exceptions: `KalshiWsError` (programming errors only; wire problems degrade `health()` and gate reads to `None`).

## Behavior

1. **One snapshot type, two transports:** `snapshot()` returns the same `OrderbookSnapshot` the REST client builds, produced through kalshi-client's `build_snapshot` from the live ladder — derived asks (`100 − other side's bid`), de-vig, and spread math stay in exactly one place. (Design deviation from the pivot sketch's separate `OrderBookSnapshot` type, on purpose: two near-identical snapshot types is a footgun, and the richer existing contract already carries everything including the full ladders the paper broker fills against.)
2. **Cent aggregation:** sub-cent levels aggregate into whole-cent buckets keyed by `dollars_to_cents`; ladder quantities floor to ints (never round available size up). Documented approximation — see kalshi-client's sub-cent note.
3. **Fail-closed on gaps:** a `seq` discontinuity (or a delta before any snapshot) marks the book `seq_gap`; `snapshot()` returns `None` until a fresh snapshot arrives (requested via `update_subscription get_snapshot`, falling back to resubscribe). A book we can't trust is a book we don't have.
4. **Reconnect:** exponential backoff to `RECONNECT_MAX_BACKOFF_S`; on reconnect every desired subscription (all subscribed tickers + BRTI) is re-sent and all books drop `have_snapshot` — nothing is served from a pre-disconnect ladder.
5. **BRTI parsing is defensive:** the upstream `data` frame's value field is probed across known keys; unparseable fields become `None` (consumers gate), and the raw frame is preserved on `BrtiState.raw` for postmortems.
6. **Health:** healthy = connected AND subscribed AND snapshot-based AND last update ≤ `STALE_BOOK_S` AND no outstanding gap.
7. **Memory bounds (24/7):** one price→qty dict per side per subscribed ticker (only the active window ±1 is ever subscribed); last BRTI tick only; no message history.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `STALE_BOOK_S` | 5.0s | book with no update this long → unhealthy |
| `RECONNECT_MAX_BACKOFF_S` | 30s | |
| `BRTI_INDEX_ID` | `BRTI` | ETH etc. subscribe their own RTI ids later |
| `KALSHI_WS_HOST_DEMO/_PROD` | see Wire facts | env overrides |

## Edge cases
- **No credentials → no stream:** paper mode without keys cannot open the WS at all (auth is mandatory); the orchestrator leaves `book=None` and consumers use REST snapshots on demand. Never a fabricated stream.
- **WS error codes 25/26/27** (buffer overflow / market limit / command rate) are logged; numeric limits are undocumented (UNVERIFIED) but this system holds 1-2 subscriptions, far below any plausible cap.
- **Late messages for unsubscribed windows** (the 15-min churn makes these routine) are dropped silently.
- **Demo books are thin/stale** (live-observed: wide quotes, sometimes an empty side) — realistic fills come from the paper broker against prod public books, not from resting demo orders.

## Dependencies
`kalshi-client` (`ws_auth_headers`, `build_snapshot`, `dollars_to_cents`), `websockets`, `orjson`. Consumed by: window-monitor agent (subscriptions), trader (decision-time book), fair-value-model (market ask), postmortem (book snapshots; sprint-4).

## Testing requirements
- All offline via `_handle_message` with explicit clocks; fixtures mirror asyncapi examples + the live 2026-07-22 KXBTC15M book.
- Snapshot → derived asks/de-vig/spread against hand-checked numbers; fractional counts floor; sub-cent levels aggregate to cent buckets.
- Delta apply/remove; gap detection fails closed then recovers on re-snapshot; delta-before-snapshot marks gap.
- Health: fresh/stale/disconnected/unsubscribed; disconnect resets snapshot trust.
- BRTI: value/avg/settlement-forming parsed; absent before first tick.
- Live behavior verified manually by `scripts/smoke_kalshi_ws.py` (auth handshake, real subscribe, message shapes).

## New types
`BookHealth`, `BrtiState` (CONTRACTS.md, sprint-2).
