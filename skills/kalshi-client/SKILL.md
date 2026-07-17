# kalshi-client

**Trigger:** any skill or agent needs Kalshi market data, orderbook pricing, fees, balance/positions, or order placement.

## What this is for

The only component in the system that talks to the Kalshi API. Everything price-shaped comes from here: de-vigged probabilities from live orderbooks, depth checks before sizing, fee estimates before entry, and the order lifecycle itself. It ports the logic of `AnitaKirkovska/kalshi-cli` (JavaScript, verified 2026-07-17) into Python — a port, not a shell-out.

## Interface

```python
get_market(market_ticker: str) -> MarketRef            # raises KalshiClientError, KalshiNotFound
get_markets(series_ticker: str, status: str | None = "open",
            date: date | None = None) -> list[MarketRef]  # paginates via cursor internally
get_orderbook(market_ticker: str) -> OrderbookSnapshot # raises KalshiClientError
depth_within(snapshot: OrderbookSnapshot, side: Side, cents_from_best: int) -> int
est_fee_cents(contracts: int, price: Cents) -> int
get_balance() -> int                                   # bankroll cents; raises KalshiAuthError
get_positions() -> list[Position]
get_fills(market_ticker: str | None = None, since: datetime | None = None) -> list[Fill]
get_settlements(market_ticker: str) -> list[Settlement]
place_order(req: OrderRequest) -> OrderResult          # raises KalshiOrderRejected, KalshiAuthError
cancel_order(order_id: str) -> OrderResult
env() -> Literal["demo", "prod"]
```

Exceptions: `KalshiClientError` (base), `KalshiAuthError`, `KalshiNotFound`, `KalshiRateLimitError`, `KalshiOrderRejected`, `KalshiProdRefused`.

## Behavior

### Hosts and environment (verified live 2026-07-17)
1. `KALSHI_ENV=demo` (default) → `https://external-api.demo.kalshi.co/trade-api/v2`. `KALSHI_ENV=prod` → `https://external-api.kalshi.com/trade-api/v2`. (`api.elections.kalshi.com/trade-api/v2` also serves prod — the host is ONE config value per env, `KALSHI_HOST_DEMO`/`KALSHI_HOST_PROD`, never inlined. Historical gotcha: older hosts/paths are dead; expect migration again.)
2. **Prod hard-refusal:** if `KALSHI_ENV=prod` and `KALSHI_ALLOW_PROD` is not exactly `"yes-i-mean-it"`, every call raises `KalshiProdRefused` at client construction. Defense in depth for the demo-only build rule.

### Auth (ported from kalshi.js, verified in source)
3. API key ID (`KALSHI_KEY_ID`) + RSA private key file (`KALSHI_KEY_PATH`). Sign `f"{timestamp_ms}{METHOD}{path}"` where `path` is from API root **without query string** (e.g. `/trade-api/v2/portfolio/balance`), RSA-PSS + SHA-256, salt length = digest length, base64. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`. Market-data GETs work unauthenticated; portfolio/order endpoints require signing. Sign everything anyway (simpler, and rate limits are friendlier authenticated).

### Pricing — orderbook truth only
4. **Never read market-object summary price fields for trading decisions.** They are deprecated/stale (confirmed both by kalshi.js comments and live: fields like `yes_ask_dollars` exist but lag the book). All pricing derives from `GET /markets/{ticker}/orderbook`.
5. **Response shape (verified live):** `{"orderbook_fp": {"yes_dollars": [["0.4400","5632.00"], ...], "no_dollars": [...]}}`. Each side lists **bids only** (resting buy orders for that side), as `[price_dollar_string, quantity_string]`, quantities may be fractional. Convert dollar-strings to integer cents immediately at the boundary: `Cents = round(Decimal(s) * 100)`; quantities floor to int contracts. Nothing outside this skill ever sees a dollar-string.
6. **Derived asks (lives here and nowhere else):** `best_yes_ask = 100 - best_no_bid`; `best_no_ask = 100 - best_yes_bid`. The full YES ask ladder is the NO bid ladder mirrored (`price → 100 - price`, same quantity). `OrderbookSnapshot.yes_book` = that derived ask ladder sorted ascending; `no_book` symmetric.
7. **De-vig:** `yes_mid = (best_yes_bid + best_yes_ask) / 2`; `no_mid = (best_no_bid + best_no_ask) / 2`; `devigged_yes_prob = yes_mid / (yes_mid + no_mid)`. One-sided book (a side has bids but no derivable ask, or vice versa): if both `best_yes_bid` and `best_no_bid` exist, mids always derive (rule 6); if either side's bid ladder is empty, set `devigged_yes_prob = None` and `spread_cents = None` — consumers must treat None as untradeable. Never substitute `last_price`.
8. `spread_cents = best_yes_ask - best_yes_bid` (None if either missing).

### Depth
9. `depth_within(snapshot, side, cents_from_best)`: sum of contracts in `side`'s ask ladder priced ≤ `best_ask + cents_from_best`. This is the number every skill's book-depth gate consumes (e.g. divergence skill: `depth_within(ob, side, 2) >= 200`).

### Fees (ported from kalshi.js; re-verify schedule in Phase 3 against docs)
10. Entry-only fee, settlement free: `fee_dollars = 0.07 * contracts * price * (1 - price)` with price in dollars; `est_fee_cents = ceil(7 * contracts * p_cents * (100 - p_cents) / 10000)` using integer math, rounded up per order. This is an *estimate* for sizing; **real** fees come from fills (`taker_fees_dollars`) and positions (`fees_paid_dollars`) and are what trade notes and P&L record.

### Orders
11. Limit orders only (`OrderRequest.limit_price`); market orders are forbidden at the client level for entries (trader rule, enforced here as: `action="buy"` requires `limit_price`). Exits may cross the spread by setting a marketable limit — still a limit order.
12. `client_order_id` passes through for idempotency; a timeout after submission triggers one status re-query by `client_order_id` before assuming failure (never double-submit).
13. `OrderResult.raw` carries the full API response for trade notes.

### Rate limiting
14. Client-side token bucket, default `KALSHI_RPS=5` req/s with burst 10, plus exponential backoff (base 1s, ×2, max 60s, jitter) on HTTP 429/5xx. 429 also raises `KalshiRateLimitError` after 3 retries so callers can degrade gracefully.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `KALSHI_ENV` | `demo` | env var |
| `KALSHI_KEY_ID`, `KALSHI_KEY_PATH` | — | env vars, required for portfolio/orders |
| `KALSHI_HOST_DEMO` | `https://external-api.demo.kalshi.co/trade-api/v2` | |
| `KALSHI_HOST_PROD` | `https://external-api.kalshi.com/trade-api/v2` | |
| `KALSHI_ALLOW_PROD` | unset | must equal `yes-i-mean-it` to allow prod |
| `KALSHI_RPS` | 5 | client-side limiter |
| `FEE_RATE` | 0.07 | named so a schedule change is one edit |

## Edge cases
- **Empty orderbook** (`orderbook_fp` missing or both ladders empty): return snapshot with all price fields None; never raise. Consumers gate on `devigged_yes_prob is None`.
- **Crossed derived book** (`best_yes_bid > derived best_yes_ask`, transient during fast moves): set `spread_cents` negative and log; consumers must not enter on a crossed book (restated in each trading skill via spread checks).
- **Fractional quantities** (`"6903.99"`): floor to int for depth; never round up available size.
- **Null/absent summary fields:** expected, not an error (rule 4).
- **`rules_secondary` → `MarketRef.settlement_notes`:** populate on `get_market`; contains postponement/cancellation terms (verified live: MLB games postponed >2 days resolve "at a fair price" — the garbage-time skill's settlement check reads this).
- **Truncated titles** ("New York Y"): titles are display-only; matching uses ticker abbreviations (league-matching's job).
- **Market `status` values:** live API returned `"active"`; treat `active`/`open` as tradeable, everything else not; unknown statuses → not tradeable + warning.
- **Clock skew:** signature timestamp must be within Kalshi's tolerance; on auth failure with correct key, re-sync from response `Date` header once before raising.

## Dependencies
None (foundation skill). Called by: league-matching, risk-management, trader agent, postmortem, garbage-time settlement checks.

## Testing requirements
- De-vig math: fixture orderbooks — two-sided normal, one-sided (no YES bids), empty, crossed, thin (1 contract) — asserting exact `devigged_yes_prob`/None.
- Derived asks: NO-bid ladder mirroring including quantity preservation and ordering.
- Fee: `est_fee_cents` table vs. hand-computed values at 1/50/99¢ × 1/100/1000 contracts; ceiling behavior at sub-cent boundaries.
- Dollar-string → cents conversion: `"0.4400"`→44, `"0.0100"`→1, fractional quantity floors.
- Prod refusal: `KALSHI_ENV=prod` without the flag raises at construction.
- Signing: known-key fixture whose signature *verifies* against the public key over the exact message `timestamp+METHOD+path` with the query string stripped. (Spec deviation, flagged 2026-07-17: RSA-PSS salts are random, so byte-for-byte reproduction is impossible; cryptographic verification is the correct equivalent.)

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
