# crypto-price-feed

**Trigger:** any component needs the current BTC/USD spot price, its short-window realized volatility, or the health of the composite feed.

`api_verified: 2026-07-22` (all five implemented WS feeds probed live, message shapes captured)
`brti_methodology_verified: 2026-07-22` (CF Benchmarks Constituent Exchanges v13.5 of 2026-06-08; RTI Methodology Guide v16.8 of 2026-06-29)

## What this is for

A streaming multi-exchange BTC/USD composite that approximates CF Benchmarks' BRTI — the index `KXBTC15M` settles on — plus a rolling realized-volatility estimator feeding the fair-value model. The settlement source is BRTI, **not any single exchange**; a single-venue spot feed would systematically mis-settle every postmortem, so this skill exists to keep model inputs on the same underlying as settlement. (Kalshi's own WS additionally streams BRTI verbatim on the `cfbenchmarks_value` channel — owned by `kalshi-ws-orderbook`; this composite is the independent cross-check and fallback, and the only vol source, since vol needs a continuous local history.)

## Interface

```python
class CryptoPriceFeed:
    async def start(self) -> None                    # opens all constituent WS connections
    async def stop(self) -> None
    def current_composite(self) -> CompositeSpot | None   # thread-safe read; None = fail closed
    def realized_vol(self, window_s: int = 900) -> float | None  # annualized; None = fail closed
    def recent_move_pct(self, window_s: int = 60) -> float | None  # signed fractional move; None = fail closed
    def health(self) -> FeedHealth                   # per-exchange last-tick age, dropout state
```

Exceptions: `CryptoPriceFeedError` (programming/configuration errors only — data problems never raise, they degrade `health()` and gate the composite to `None`).

## BRTI constituents (configuration, not code constants)

Current BRTI constituent list per CF Benchmarks' Constituent Exchanges doc v13.5 (2026-06-08). BRTI has **no per-venue weights** — it merges all books into one consolidated book — so implemented constituents get equal weight 1.0 in the median by design.

| Constituent | In BRTI since | Implemented | WS endpoint | Notes |
|---|---|---|---|---|
| Coinbase | 2016-11-14 | yes | `wss://advanced-trade-ws.coinbase.com` | `ticker` channel, `BTC-USD`; subscribe within 5s of connect |
| Kraken | 2016-11-14 | yes | `wss://ws.kraken.com/v2` | v2 `ticker`, `BTC/USD`, `event_trigger: bbo` |
| Bitstamp | 2016-11-14 | yes | `wss://ws.bitstamp.net` | no ticker channel; top of `order_book_btcusd` snapshots |
| Gemini | 2019-08-30 | yes | `wss://api.gemini.com/v1/marketdata/btcusd?top_of_book=true` | v1 (v2 `l2_updates` carries no timestamps); stateful TOB parse |
| LMAX Digital | 2021-05-03 | yes | `wss://public-data-api.london-digital.lmax.com/v1/web-socket` | public Data API; throttled to 1 update/s; instrument `btc-usd` |
| itBit (Paxos) | 2016-11-14 | no | — | public API exists; add when >5 constituents wanted |
| Bullish Exchange | 2024-12-30 | no | — | recent addition to BRTI |
| Crypto.com | 2025-03-31 | no | — | recent addition to BRTI |

Suspended (not constituents): Bitfinex, OKCoin (both since Apr-2017). When CF Benchmarks revises the list, update this table and `default_constituents()` together, and re-stamp `brti_methodology_verified`.

## Behavior

### Composite
1. Composite mid = **weighted median** of healthy constituents' top-of-book mids (bid and ask medianed the same way). Median, not mean: robust to a single venue going stale or spiking — one bad venue moves a mean, not a median.
2. A constituent is **healthy** iff its WS session is connected AND its last tick is ≤ `STALE_CONSTITUENT_S` old. Disconnection makes a venue unhealthy immediately, even if its last tick is recent.
3. **Deviation gate** (BRTI's "potentially erroneous data" rule, §5 of the methodology, parameter 5%): after the health filter, any venue whose mid deviates > `MAX_MID_DEVIATION` from the median of healthy mids is excluded from the composite. Simplification vs. BRTI: stateless per computation — no 2.5% re-entry hysteresis. With exactly two healthy venues that mutually deviate, both fall outside the gate and the composite fails closed (correct: we can't tell which is wrong).
4. **Fail-closed:** fewer than `MIN_HEALTHY_CONSTITUENTS` healthy venues (before or after the deviation gate) → `current_composite()` returns `None` and consumers must decline. Never falls back to a single exchange silently.
5. Per-venue book sanity mirrors BRTI §5: one-sided books and internally crossed quotes (`bid >= ask`) are dropped at parse time — the venue's previous quote stands until it goes stale.

### Divergence from BRTI (known, accepted)
BRTI is computed every 200ms from a consolidated, size-capped, uncrossed, depth-weighted book across 8 venues (exponentially weighted mid price-volume curve to 0.5% deviation-from-mid utilized depth; disseminated top-of-second). This skill's top-of-book median over 5 of those 8 venues tracks it closely in calm markets and diverges most when books are thin or crossed across venues. That residual basis is why the fair-value consumer must also cross-check against the `cfbenchmarks_value` BRTI stream when available, and why postmortems record both.

### Realized vol
6. `realized_vol(window_s)` = annualized **population** std of log returns of the 1-second-resampled composite mid over the trailing window: each return is normalized by `sqrt(dt)` (so gaps from unhealthy periods widen `dt` rather than fabricating a return), and annualization is `* sqrt(31_536_000)`.
7. Sampling: an internal 1s task appends `(monotonic_ts, mid)` when — and only when — the composite is available. Unhealthy seconds leave a gap, never a stale or interpolated point.
8. Fail-closed: `None` until the window holds ≥ `max(2, min(MIN_VOL_SAMPLES, MIN_VOL_COVERAGE·window_s/SAMPLE_INTERVAL_S))` samples spanning ≥ `MIN_VOL_COVERAGE` of `window_s`. The floor scales with the window because a 60s window can only ever hold ~59 one-second samples — a fixed 60-sample floor would make small windows permanently unanswerable (found live in the sprint-1 smoke run). Plausibility gating (e.g. 20%–200% annualized) is the *consumer's* job (trading-skill entry conditions), not this skill's.

### Recent move (velocity)
8b. `recent_move_pct(window_s)` = signed fractional change of the composite mid between the latest sample and the sample at (or just after) `now - window_s`, drawn from the same 1s-resampled buffer `realized_vol` uses (no separate history is kept). Consumed by risk-management's entry-sizing velocity scale (`VELOCITY_THRESHOLD_PCT`/`VELOCITY_SIZE_SCALE`) to size smaller into a spot that's moving unusually fast right now, independent of `realized_vol`'s longer-window estimate. Fail-closed: `None` if the buffer doesn't yet cover the full window, or if the latest sample is more than `2 * SAMPLE_INTERVAL_S` old (feed stalled) — a consumer seeing `None` simply skips the scale-down rather than guessing.

### Memory bounds (24/7 hygiene)
9. Per constituent: last quote only. Vol buffer: one `deque(maxlen=MAX_SAMPLES)` of `(float, float)` tuples — ~3700 entries ≈ 1h window + slack. No per-tick history is stored anywhere; a week-long run holds the same memory as a minute-long one.

### Reconnection
10. Each constituent runs an independent connect/subscribe/read loop with exponential backoff (1s doubling to `RECONNECT_MAX_BACKOFF_S`). Reconnects are per-venue; one venue flapping never touches the others. `stop()` cancels all tasks and is idempotent-safe to call once.
11. `ConstituentSpec.ping_interval` (default 20s, the `websockets` library default) is a per-venue override for client-initiated WS keepalive pings, passed straight through to `websockets.connect`. LMAX sets it to `None` (found live 2026-07-30): its server was closing with `1008 policy violation: Excessive pings received` against the library's default ping cadence, causing repeated reconnects that flapped it in and out of `STALE_CONSTITUENT_S` health. Disabling the client ping for LMAX is safe because liveness there is still covered by this skill's own staleness check (last-tick age), independent of WS-level ping/pong.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `STALE_CONSTITUENT_S` | 2.0s | > LMAX's 1s throttle, << BRTI's own 10s retrieval-lag threshold |
| `MIN_HEALTHY_CONSTITUENTS` | 2 | below → composite `None` |
| `MAX_MID_DEVIATION` | 0.05 | BRTI potentially-erroneous-data parameter for BRTI |
| `SAMPLE_INTERVAL_S` | 1.0s | vol resample resolution (matches BRTI dissemination) |
| `DEFAULT_VOL_WINDOW_S` | 900s | matches the 15-minute contract window |
| `MIN_VOL_SAMPLES` | 60 | |
| `MIN_VOL_COVERAGE` | 0.5 | of the requested window |
| `MAX_SAMPLES` | 3700 | vol deque bound |
| `RECONNECT_MAX_BACKOFF_S` | 30s | |
| `VELOCITY_WINDOW_S` | 60s | short window for `recent_move_pct` |

## Edge cases
- **Gemini stateful parse:** v1 `top_of_book` events replace the tracked side only when `remaining > 0`; a top-of-book removal is followed by the new top as its own event. A transiently crossed tracked pair is skipped (no tick), not published.
- **LMAX 1Hz throttle + daily close:** LMAX throttles public updates to 1/s (comfortably inside the 2s staleness bound) and most LMAX instruments close ~5 min daily — during that window LMAX simply goes stale and the median proceeds on the remaining venues.
- **Bitstamp has no BBO channel:** top of the 100-level `order_book_btcusd` snapshot is used; `microtimestamp` (µs epoch string) is the source timestamp.
- **Coinbase idle disconnect:** the server drops connections with no subscription within 5s; subscribe is sent immediately on connect.
- **Change-driven feeds flap "stale" in quiet moments (observed live, sprint-1 smoke):** Coinbase `ticker` fires per match, Kraken `bbo` and Gemini `top_of_book` fire per BBO change — a quiet second or two produces no message even though the venue's quote is still valid. With `STALE_CONSTITUENT_S=2` these venues transiently drop out and the median proceeds on the rest; that is accepted behavior. If a 24h paper run shows spurious whole-composite fail-closes in deep overnight quiet, the knob is this parameter (BRTI's own retrieval-lag threshold is 10s), not code.
- **Clock basis:** staleness uses the process monotonic clock (`fetched`-side, per CONTRACTS conventions); exchange `source_ts` is recorded for observability/postmortems but never used for staleness.
- **BTC only for now:** constituent specs are configuration; an ETH feed is `default_constituents()` with different products, not new code. Nothing in this module hard-codes BTC beyond the default spec table.

## Dependencies
`websockets`, `orjson`, `numpy` (pyproject). No dependency on other skills. Consumed by: `fair-value-model` (spot + sigma), `window-monitor` (spot context), `postmortem` (window spot log), dashboard (health).

## Testing requirements
- Weighted median: 3/4/5 constituents, equal and dominant weights, exact half-weight straddle averaging, outlier robustness.
- Composite: median of three; stale constituent excluded; deviation gate exclusion (>5% mid) and mutual-deviation fail-closed; below-min-healthy → `None`; disconnected-but-recent → unhealthy.
- Vol: hand-computed annualized number from a fixture of known 1-second returns (alternating ±r → exactly `r*sqrt(31536000)`); constant price → 0.0; gap normalization (dt=2 path scales by `1/sqrt(2)`); insufficient coverage → `None`; unhealthy seconds leave gaps, not garbage.
- Memory: sample buffer respects `maxlen` under a long synthetic run.
- All offline: ticks injected via `_on_tick`/`_sample_once` with explicit clocks; no WS in tests. Live behavior is verified by `scripts/smoke_price_feed.py` (manual, 60s).

## New types
`CompositeSpot`, `ConstituentHealth`, `FeedHealth` (added to CONTRACTS.md, sprint-1).
