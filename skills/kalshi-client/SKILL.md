# kalshi-client

**Trigger:** any skill or agent needs Kalshi market data, orderbook pricing, fees, balance/positions, WebSocket auth headers, or order placement.

## What this is for

The only component in the system that talks to the Kalshi API. Everything price-shaped comes from here: de-vigged probabilities from live orderbooks, depth checks before sizing, fee estimates before entry, and the order lifecycle itself. In the crypto build it additionally owns connect-time auth for the market-data WebSocket (consumed by kalshi-ws-orderbook) and the whole-cent aggregation of Kalshi's sub-cent book (KXBTC15M reality, see rule 6). Auth and de-vig logic ports `AnitaKirkovska/kalshi-cli` (JavaScript, verified 2026-07-17) into Python — a port, not a shell-out.

## Interface

```python
get_market(market_ticker: str, family: str = "") -> MarketRef   # raises KalshiClientError, KalshiNotFound
get_market_raw(market_ticker: str) -> dict                      # full market object; NEVER for pricing
get_markets(series_ticker: str, status: str | None = "open",
            family: str = "") -> list[MarketRef]                # paginates via cursor internally
get_orderbook(market: MarketRef) -> OrderbookSnapshot           # raises KalshiClientError
build_snapshot(market: MarketRef, orderbook_raw: dict,
               fetched_at: datetime | None = None) -> OrderbookSnapshot  # pure, module-level
depth_within(snapshot: OrderbookSnapshot, side: Side, cents_from_best: int) -> int
est_fee_cents(contracts: int, price: Cents) -> int
dollars_to_cents(s: str | float) -> Cents                       # boundary conversion, module-level
get_balance() -> int                                            # bankroll cents; raises KalshiAuthError
get_positions() -> list[Position]
get_fills(market_ticker: str | None = None) -> list[Fill]
get_settlements(market_ticker: str) -> list[Settlement]
place_order(req: OrderRequest) -> OrderResult                   # raises KalshiOrderRejected, KalshiAuthError
cancel_order(order_id: str) -> OrderResult
ws_auth_headers(ws_path: str = "/trade-api/ws/v2") -> dict      # raises KalshiAuthError if key unset
env() -> Literal["demo", "prod"]
```

Exceptions: `KalshiClientError` (base), `KalshiAuthError`, `KalshiNotFound`, `KalshiRateLimitError`, `KalshiOrderRejected`, `KalshiProdRefused`.

## Behavior

### Hosts and environment (verified live 2026-07-17)
1. `KALSHI_ENV=demo` (default) → `https://external-api.demo.kalshi.co/trade-api/v2`. `KALSHI_ENV=prod` → `https://external-api.kalshi.com/trade-api/v2`. (`api.elections.kalshi.com/trade-api/v2` also serves prod — the host is ONE config value per env, `KALSHI_HOST_DEMO`/`KALSHI_HOST_PROD`, never inlined. Historical gotcha: older hosts/paths are dead; expect migration again.) Any other `KALSHI_ENV` value raises `KalshiClientError` at construction.
2. **Prod hard-refusal:** if `KALSHI_ENV=prod` and `KALSHI_ALLOW_PROD` is not exactly `"yes-i-mean-it"`, the client raises `KalshiProdRefused` at construction. Defense in depth for the demo-only build rule.

### Auth (ported from kalshi.js, verified in source)
3. API key ID (`KALSHI_KEY_ID`) + RSA private key file (`KALSHI_KEY_PATH`). Sign `f"{timestamp_ms}{METHOD}{path}"` where `path` is from API root **without query string** (e.g. `/trade-api/v2/portfolio/balance`), RSA-PSS + SHA-256, salt length = digest length, base64. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`. Market-data GETs work unauthenticated; portfolio/order endpoints require signing (missing key → `KalshiAuthError` before the request goes out). Sign everything anyway when a key is present (simpler, and rate limits are friendlier authenticated).
4. **WebSocket auth (verified against docs.kalshi.com quick-start, 2026-07-22):** `ws_auth_headers(ws_path)` returns connect-time headers for the market-data WebSocket — the same RSA-PSS scheme signed over `timestamp + "GET" + ws_path`. The WS connection itself always requires auth; there is no public channel. Raises `KalshiAuthError` if no key is configured.

### Pricing — orderbook truth only
5. **Never read market-object summary price fields for trading decisions.** They are deprecated/stale (confirmed both by kalshi.js comments and live: fields like `yes_ask_dollars` exist but lag the book). All pricing derives from `GET /markets/{ticker}/orderbook`. `get_market_raw` exists precisely for the *non-price* fields `MarketRef` doesn't carry — `floor_strike`, `status`, open/close times — window-monitor's window-verification path; it is never a pricing source.
6. **Response shape and sub-cent reality (verified live 2026-07-22):** `{"orderbook_fp": {"yes_dollars": [["0.4400","5632.00"], ...], "no_dollars": [...]}}` (legacy `orderbook`/`yes`/`no` keys tolerated). Each side lists **bids only** (resting buy orders for that side), as `[price_dollar_string, quantity_string]`. KXBTC15M uses the `tapered_deci_cent` price structure — $0.001 ticks below $0.10 and above $0.90 — and fractional fp contract counts. Convert at the boundary: prices via `dollars_to_cents` (`round(Decimal(s) * 100)`), aggregating deci-cent ticks to whole cents (documented approximation); quantities **floor** to int contracts, never round up; zero-quantity levels are dropped. Order prices we submit are whole cents, which are valid ticks in every band. Nothing outside this skill ever sees a dollar-string.
7. **Derived asks (lives here and nowhere else):** `best_yes_ask = 100 - best_no_bid`; `best_no_ask = 100 - best_yes_bid`. The full YES ask ladder is the NO bid ladder mirrored (`price → 100 - price`, same quantity). `OrderbookSnapshot.yes_book` = that derived ask ladder sorted ascending; `no_book` symmetric.
8. **De-vig:** `yes_mid = (best_yes_bid + best_yes_ask) / 2`; `no_mid = (best_no_bid + best_no_ask) / 2`; `devigged_yes_prob = yes_mid / (yes_mid + no_mid)`. If both `best_yes_bid` and `best_no_bid` exist, mids always derive (rule 7); if either side's bid ladder is empty, set `devigged_yes_prob = None` and `spread_cents = None` — consumers must treat None as untradeable. Never substitute `last_price`.
9. `spread_cents = best_yes_ask - best_yes_bid` (None if either missing).
10. **`build_snapshot` is a pure module-level function** — raw orderbook payload in, `OrderbookSnapshot` out — so the kalshi-ws-orderbook skill builds the *same* snapshot type from its live WS ladder. One snapshot type, one derivation, both data paths (REST and WS).

### Depth
11. `depth_within(snapshot, side, cents_from_best)`: sum of contracts in `side`'s ask ladder priced ≤ `best_ask + cents_from_best`. This is the number every book-depth gate consumes (e.g. the fair-value skill's minimum-depth entry check).

### Fees (ported from kalshi.js; schedule re-verified quadratic with `fee_multiplier` 1 for KXBTC15M, 2026-07-22)
12. Entry-only fee, settlement free: `fee_dollars = 0.07 * contracts * price * (1 - price)` with price in dollars; `est_fee_cents = ceil(7 * contracts * p_cents * (100 - p_cents) / 10000)` using integer math, rounded up per order; 0 for non-positive contract counts. This is an *estimate* for sizing; **real** fees come from fills (`fee_cost`, a dollar string — verified live 2026-07-24; the fill object has NO `taker_fees_dollars`, that key lives only on the *order* object per rule 16) and positions (`fees_paid_dollars`) and are what trade notes and P&L record. `get_fills` falls back to the legacy `taker_fees_dollars` key for safety.

### Orders
13. Limit orders only; market orders are forbidden at the client level for entries (trader rule, enforced here as: `action="buy"` requires `limit_price`, else `KalshiOrderRejected`). Exits may cross the spread by setting a marketable limit — still a limit order.
14. **v2 order placement (verified live 2026-07-18):** `POST /portfolio/events/orders`. The v2 book is YES-only — `"bid"` buys YES at `limit_price`; `"ask"` sells YES at `100 - limit_price`, which is economically equivalent to buying NO (see create-order-v2 BookSide spec). Body sends `count` as an fp string (`"5.00"` — we always submit whole contracts), `price` as a 4-decimal dollar string, `type="limit"`, `time_in_force="immediate_or_cancel"`, `self_trade_prevention_type="taker_at_cross"`, plus `client_order_id`.
15. `client_order_id` passes through for idempotency (generated by the trader).
16. **Two incompatible order-shaped responses, both verified live 2026-07-18, both handled:** POST create-order returns a *flat* `{fill_count, average_fill_price, average_fee_paid, remaining_count, order_id}` with **no** `status` field; GET/DELETE `/portfolio/orders/{id}` returns `{"order": {fill_count_fp, taker_fill_cost_dollars, maker_fill_cost_dollars, taker_fees_dollars, maker_fees_dollars, status, ...}}`. When `status` is absent it is inferred: fills > 0 → `filled`; else `remaining_count == 0` → `canceled`; else `resting`. Regression note: handling only the GET shape silently reported `filled_contracts=0` for every real fill on the POST path — the one `place_order` actually uses. Do not repeat.
17. Order-placement API errors that are not auth or rate-limit failures are wrapped as `KalshiOrderRejected`. `OrderResult.raw` carries the full API response for trade notes.

### Rate limiting
18. Client-side token bucket, default `KALSHI_RPS=5` req/s with burst 10, plus exponential backoff (base 1s, ×2, max 60s) on HTTP 429/5xx — 4 attempts total, then `KalshiRateLimitError` (429) or `KalshiClientError` (5xx) so callers can degrade gracefully. 404 → `KalshiNotFound`; 401/403 → `KalshiAuthError` immediately (no retry).

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `KALSHI_ENV` | `demo` | env var |
| `KALSHI_KEY_ID`, `KALSHI_KEY_PATH` | — | env vars, required for portfolio/orders/WS |
| `KALSHI_HOST_DEMO` | `https://external-api.demo.kalshi.co/trade-api/v2` | verified live 2026-07-17 |
| `KALSHI_HOST_PROD` | `https://external-api.kalshi.com/trade-api/v2` | verified live 2026-07-17 |
| `KALSHI_ALLOW_PROD` | unset | must equal `yes-i-mean-it` to allow prod |
| `KALSHI_RPS` | 5 (burst 10) | CONFIRMED 2026-07-17 (category-agnostic, unchanged) |
| `FEE_RATE` | 0.07 | CONFIRMED 2026-07-17; named so a schedule change is one edit; re-verified quadratic, `fee_multiplier` 1 for KXBTC15M 2026-07-22 |
| WS path | `/trade-api/ws/v2` | PROPOSED 2026-07-22 (crypto pivot default, pending owner confirmation); `ws_auth_headers` default arg |

## Edge cases
- **Empty orderbook** (`orderbook_fp` missing or both ladders empty): return snapshot with all price fields None; never raise. Consumers gate on `devigged_yes_prob is None`.
- **Crossed derived book** (`best_yes_bid > derived best_yes_ask`, transient during fast moves): `spread_cents` goes negative; consumers must not enter on a crossed book (restated in each trading skill via spread checks).
- **Fractional fp quantities** (`"6903.99"`): floor to int for depth and fills; never round up available size.
- **Deci-cent price levels** (`"0.0450"`, tail bands only): aggregate to the nearest whole cent at the boundary — the documented approximation of rule 6.
- **Null/absent summary fields:** expected, not an error (rule 5).
- **`rules_secondary` → `MarketRef.settlement_notes`:** populated on `get_market`/`get_markets`; carries the market's secondary rules text (void/early-close terms) for trade notes and settlement audits.
- **Ticker grammar → `yes_label`:** taken from `yes_sub_title` when present, else the ticker suffix after `event_ticker + "-"`; empty string when the ticker doesn't follow that grammar. Titles are display-only; window resolution keys on tickers (window-monitor's job).
- **Market `status` values:** exported constant `TRADEABLE_STATUSES = {"active", "open"}` — treat those as tradeable, everything else (including unknown values) as not tradeable.
- **Balance field migration:** prefer the integer-cents `balance` field; fall back to `balance_dollars` via `dollars_to_cents`.
- **Fill parsing:** price read from the side-specific dollar field (`yes_price_dollars`/`no_price_dollars`, fallback `price_dollars`); fee read from `fee_cost` (dollar string, fallback legacy `taker_fees_dollars`), recorded as absolute cents.
- **Clock skew:** the signature timestamp must be within Kalshi's tolerance; a skewed clock surfaces as `KalshiAuthError` with a correct key.

## Dependencies
None (foundation skill). Called by: window-monitor (`get_market_raw` verification, market discovery), kalshi-ws-orderbook (`ws_auth_headers`, `build_snapshot`, `dollars_to_cents`), risk-management, trader agent, postmortem, paper broker, orchestrator.

## Testing requirements
- De-vig math: fixture orderbooks — two-sided normal, one-sided (no YES bids), empty, crossed, thin (1 contract) — asserting exact `devigged_yes_prob`/None.
- Derived asks: NO-bid ladder mirroring including quantity preservation and ordering.
- Fee: `est_fee_cents` table vs. hand-computed values at 1/50/99¢ × 1/100/1000 contracts; ceiling behavior at sub-cent boundaries.
- Dollar-string → cents conversion: `"0.4400"`→44, `"0.0100"`→1, fractional fp quantity floors.
- Prod refusal: `KALSHI_ENV=prod` without the flag raises at construction; `demo` default constructs.
- Signing: known-key fixture whose signature *verifies* against the public key over the exact message `timestamp+METHOD+path` with the query string stripped. (Spec deviation, flagged 2026-07-17: RSA-PSS salts are random, so byte-for-byte reproduction is impossible; cryptographic verification is the correct equivalent.)
- Order-result shapes: real captured responses for *both* shapes (flat POST create-order and nested GET order) report the actual fill count and fees; a zero-fill flat response is not `filled`; `place_order` end-to-end parses the real flat response (regression guard for rule 16).
- v2 placement mapping: buy YES posts book side `"bid"` at `limit_price`; buy NO posts `"ask"` at `100 - limit_price`; a deprecated-endpoint rejection raises `KalshiOrderRejected`.
- Fill fees: a real captured fill row reports its `fee_cost` as non-zero `taker_fee_cents` (regression guard — the wrong key silently zeroed every fill's fee); a missing fee key yields 0; the legacy `taker_fees_dollars` key is still honored as a fallback.

## New types
```python
@dataclass
class Position:
    market_ticker: str; side: Side; contracts: int
    avg_price: Cents; fees_paid_cents: int; raw: dict

@dataclass
class Fill:
    order_id: str; market_ticker: str; side: Side; action: str
    contracts: int; price: Cents; taker_fee_cents: int; ts: datetime; raw: dict

@dataclass
class Settlement:
    market_ticker: str; result: Literal["yes", "no", "void"]
    settled_ts: datetime | None; revenue_cents: int; raw: dict
```
