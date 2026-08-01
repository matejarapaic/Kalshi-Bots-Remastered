"""crypto-price-feed skill. Spec: skills/crypto-price-feed/SKILL.md.

Streaming multi-exchange BTC/USD spot composite approximating CF Benchmarks'
BRTI (the KXBTC15M settlement source), plus a rolling realized-volatility
estimator. Five of the eight current BRTI constituents are implemented (see
SKILL.md config table for the full list and verification dates).

Composite = weighted median of healthy constituents' top-of-book mids, after
excluding venues whose mid deviates > MAX_MID_DEVIATION from the median of
venue mids (BRTI's "potentially erroneous data" rule, simplified: stateless,
no re-entry hysteresis). Median, not mean: robust to a single venue going
stale or spiking. This is a top-of-book approximation of BRTI's depth-weighted
consolidated-book calculation — see SKILL.md "Divergence from BRTI" for the
known error modes.

Fail-closed: fewer than MIN_HEALTHY_CONSTITUENTS healthy venues (or fewer
surviving the deviation gate) -> current_composite() returns None and every
consumer must decline. Never falls back to a single exchange silently.

Memory bounds (24/7 hygiene): per constituent, only the last quote is kept;
the vol buffer is a deque bounded at MAX_SAMPLES (1h window + slack). No
per-tick history is stored anywhere.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import numpy as np

from kalshi_bots.types import CompositeSpot, ConstituentHealth, FeedHealth

log = logging.getLogger(__name__)

# --- named parameters (spec: crypto-price-feed/SKILL.md config table) ---
STALE_CONSTITUENT_S = 2.0        # last tick older than this -> unhealthy
MIN_HEALTHY_CONSTITUENTS = 2     # below this the composite fails closed
MAX_MID_DEVIATION = 0.05         # BRTI potentially-erroneous-data parameter
SAMPLE_INTERVAL_S = 1.0          # vol buffer resolution (1s resample)
DEFAULT_VOL_WINDOW_S = 900       # matches the 15-minute contract window
MIN_VOL_SAMPLES = 60             # fail-closed: too few samples -> None
MIN_VOL_COVERAGE = 0.5           # samples must span >= half the window
MAX_SAMPLES = 3700               # deque bound: 3600s window + slack
SECONDS_PER_YEAR = 31_536_000    # 365d; tau uses the same base in fair-value
RECONNECT_MAX_BACKOFF_S = 30.0
VELOCITY_WINDOW_S = 60           # short window for "how fast is spot moving right now"


class CryptoPriceFeedError(Exception):
    pass


# parse(msg, scratch) -> (bid, ask, source_ts) or None. `scratch` is a
# per-connection dict for parsers that must track state (Gemini top-of-book).
Parser = Callable[[dict, dict], "tuple[float, float, datetime | None] | None"]


@dataclass
class ConstituentSpec:
    name: str
    weight: float
    ws_url: str
    subscribe: dict | None       # JSON payload sent on connect; None = none needed
    parse: Parser | None
    ping_interval: float | None = 20  # None = no client-initiated pings; a
                                       # venue's own staleness (last-tick age
                                       # vs STALE_CONSTITUENT_S) still detects
                                       # a dead connection independent of this


@dataclass
class _ConstState:
    bid: float = 0.0
    ask: float = 0.0
    source_ts: datetime | None = None
    last_tick_mono: float | None = None
    connected: bool = False


def weighted_median(pairs: list[tuple[float, float]]) -> float:
    """Weighted median of (value, weight) pairs. When the cumulative weight
    lands exactly on half the total, average the two straddling values."""
    if not pairs:
        raise CryptoPriceFeedError("weighted_median of empty set")
    ordered = sorted(pairs)
    half = sum(w for _, w in ordered) / 2.0
    cum = 0.0
    for i, (value, w) in enumerate(ordered):
        cum += w
        if math.isclose(cum, half) and i + 1 < len(ordered):
            return (value + ordered[i + 1][0]) / 2.0
        if cum >= half:
            return value
    return ordered[-1][0]  # unreachable barring float dust


def _iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _valid_quote(bid: float, ask: float) -> bool:
    """BRTI drops one-sided and internally-crossed venue books; so do we."""
    return 0 < bid < ask


# --- per-exchange parsers (formats live-verified 2026-07-22, see SKILL.md) ---

def parse_coinbase(msg: dict, scratch: dict):
    if msg.get("channel") != "ticker":
        return None
    ts = _iso(msg.get("timestamp"))
    for ev in msg.get("events", []):
        for t in ev.get("tickers", []):
            if t.get("product_id") != "BTC-USD":
                continue
            try:
                bid, ask = float(t["best_bid"]), float(t["best_ask"])
            except (KeyError, TypeError, ValueError):
                continue
            if _valid_quote(bid, ask):
                return bid, ask, ts
    return None


def parse_kraken(msg: dict, scratch: dict):
    if msg.get("channel") != "ticker" or not msg.get("data"):
        return None
    d = msg["data"][0]
    try:
        bid, ask = float(d["bid"]), float(d["ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if not _valid_quote(bid, ask):
        return None
    return bid, ask, _iso(d.get("timestamp"))


def parse_bitstamp(msg: dict, scratch: dict):
    if msg.get("event") != "data" or not str(msg.get("channel", "")).startswith("order_book"):
        return None
    d = msg.get("data") or {}
    bids, asks = d.get("bids") or [], d.get("asks") or []
    if not bids or not asks:
        return None  # one-sided book: dropped
    try:
        bid, ask = float(bids[0][0]), float(asks[0][0])
    except (IndexError, TypeError, ValueError):
        return None
    if not _valid_quote(bid, ask):
        return None
    ts = None
    if d.get("microtimestamp"):
        try:
            ts = datetime.fromtimestamp(int(d["microtimestamp"]) / 1e6, tz=timezone.utc)
        except (TypeError, ValueError):
            ts = None
    return bid, ask, ts


def parse_gemini(msg: dict, scratch: dict):
    """v1 marketdata with top_of_book=true: stateful — each change event's
    price is the new top of book for its side; remaining==0 removals are
    skipped (the replacement top arrives as its own event)."""
    if msg.get("type") != "update":
        return None
    ts = None
    if msg.get("timestampms"):
        try:
            ts = datetime.fromtimestamp(int(msg["timestampms"]) / 1e3, tz=timezone.utc)
        except (TypeError, ValueError):
            ts = None
    for ev in msg.get("events", []):
        if ev.get("type") != "change" or ev.get("side") not in ("bid", "ask"):
            continue
        try:
            price, remaining = float(ev["price"]), float(ev["remaining"])
        except (KeyError, TypeError, ValueError):
            continue
        if remaining > 0:
            scratch[ev["side"]] = price
    bid, ask = scratch.get("bid"), scratch.get("ask")
    if bid is None or ask is None or not _valid_quote(bid, ask):
        return None
    return bid, ask, ts


def parse_lmax(msg: dict, scratch: dict):
    if msg.get("type") != "ORDER_BOOK" or msg.get("instrument_id") != "btc-usd":
        return None
    bids, asks = msg.get("bids") or [], msg.get("asks") or []
    if not bids or not asks:
        return None  # one-sided book: dropped
    try:
        bid, ask = float(bids[0]["price"]), float(asks[0]["price"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not _valid_quote(bid, ask):
        return None
    return bid, ask, _iso(msg.get("timestamp"))


def default_constituents() -> list[ConstituentSpec]:
    """The implemented subset of BRTI's current constituent list (5 of 8 —
    itBit, Bullish, Crypto.com are documented-but-unimplemented; SKILL.md).
    Equal weights: BRTI itself has no per-venue weights (it consolidates
    books), so the median is unweighted by design."""
    return [
        ConstituentSpec(
            name="coinbase", weight=1.0,
            ws_url="wss://advanced-trade-ws.coinbase.com",
            subscribe={"type": "subscribe", "product_ids": ["BTC-USD"],
                       "channel": "ticker"},
            parse=parse_coinbase),
        ConstituentSpec(
            name="kraken", weight=1.0,
            ws_url="wss://ws.kraken.com/v2",
            subscribe={"method": "subscribe",
                       "params": {"channel": "ticker", "symbol": ["BTC/USD"],
                                  "event_trigger": "bbo"}},
            parse=parse_kraken),
        ConstituentSpec(
            name="bitstamp", weight=1.0,
            ws_url="wss://ws.bitstamp.net",
            subscribe={"event": "bts:subscribe",
                       "data": {"channel": "order_book_btcusd"}},
            parse=parse_bitstamp),
        ConstituentSpec(
            name="gemini", weight=1.0,
            ws_url="wss://api.gemini.com/v1/marketdata/btcusd?top_of_book=true",
            subscribe=None,
            parse=parse_gemini),
        ConstituentSpec(
            name="lmax", weight=1.0,
            ws_url="wss://public-data-api.london-digital.lmax.com/v1/web-socket",
            subscribe={"type": "SUBSCRIBE",
                       "channels": [{"name": "ORDER_BOOK",
                                     "instruments": ["btc-usd"]}]},
            parse=parse_lmax,
            # LMAX's server closes with 1008 "Excessive pings received" against
            # the websockets library's default client-side keepalive ping
            # (found live 2026-07-30) -- disable our ping, rely on its own
            # traffic plus our staleness check for liveness.
            ping_interval=None),
    ]


class CryptoPriceFeed:
    """Reads are thread-safe (dashboard reads from another thread); writes
    happen on the asyncio loop that ran start()."""

    def __init__(self, constituents: list[ConstituentSpec] | None = None):
        self.specs = constituents or default_constituents()
        self._weights = {s.name: s.weight for s in self.specs}
        self._state: dict[str, _ConstState] = {s.name: _ConstState() for s in self.specs}
        self._samples: deque[tuple[float, float]] = deque(maxlen=MAX_SAMPLES)
        self._lock = threading.Lock()
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    # --- streaming lifecycle ---

    async def start(self) -> None:
        if self._tasks:
            raise CryptoPriceFeedError("feed already started")
        self._stopping = False
        self._tasks = [asyncio.create_task(self._run_constituent(s),
                                           name=f"feed-{s.name}")
                       for s in self.specs]
        self._tasks.append(asyncio.create_task(self._run_sampler(),
                                               name="feed-sampler"))

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _run_constituent(self, spec: ConstituentSpec) -> None:
        import orjson
        import websockets
        backoff = 1.0
        while not self._stopping:
            try:
                async with websockets.connect(spec.ws_url, open_timeout=10,
                                              close_timeout=5,
                                              ping_interval=spec.ping_interval) as ws:
                    if spec.subscribe is not None:
                        await ws.send(orjson.dumps(spec.subscribe).decode())
                    scratch: dict = {}
                    backoff = 1.0
                    async for raw in ws:
                        msg = orjson.loads(raw)
                        parsed = spec.parse(msg, scratch)
                        if parsed is not None:
                            self._on_tick(spec.name, *parsed)
            except asyncio.CancelledError:
                self._on_disconnect(spec.name)
                raise
            except Exception as e:
                log.warning("%s feed dropped: %s", spec.name, e)
            self._on_disconnect(spec.name)
            if self._stopping:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF_S)

    async def _run_sampler(self) -> None:
        while not self._stopping:
            await asyncio.sleep(SAMPLE_INTERVAL_S)
            self._sample_once()

    # --- tick ingestion (tests drive these directly with explicit clocks) ---

    def _on_tick(self, name: str, bid: float, ask: float,
                 source_ts: datetime | None = None, mono: float | None = None) -> None:
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            st = self._state[name]
            st.bid, st.ask = bid, ask
            st.source_ts = source_ts
            st.last_tick_mono = mono
            st.connected = True

    def _on_disconnect(self, name: str) -> None:
        with self._lock:
            self._state[name].connected = False

    def _sample_once(self, mono: float | None = None,
                     wall: datetime | None = None) -> None:
        mono = time.monotonic() if mono is None else mono
        spot = self.current_composite(mono=mono)
        if spot is None:
            return  # gap, not garbage: vol math normalizes over the hole
        with self._lock:
            self._samples.append((mono, spot.mid))

    # --- reads ---

    def _fresh(self, mono: float) -> list[tuple[str, _ConstState]]:
        return [(n, st) for n, st in self._state.items()
                if st.connected and st.last_tick_mono is not None
                and mono - st.last_tick_mono <= STALE_CONSTITUENT_S]

    def current_composite(self, mono: float | None = None) -> CompositeSpot | None:
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            fresh = self._fresh(mono)
            if len(fresh) < MIN_HEALTHY_CONSTITUENTS:
                return None
            mids = {n: (st.bid + st.ask) / 2.0 for n, st in fresh}
            median0 = weighted_median([(m, self._weights[n]) for n, m in mids.items()])
            used = [(n, st) for n, st in fresh
                    if abs(mids[n] - median0) / median0 <= MAX_MID_DEVIATION]
            if len(used) < MIN_HEALTHY_CONSTITUENTS:
                return None  # venues disagree too much to trust any of them
            return CompositeSpot(
                mid=weighted_median([(mids[n], self._weights[n]) for n, _ in used]),
                bid=weighted_median([(st.bid, self._weights[n]) for n, st in used]),
                ask=weighted_median([(st.ask, self._weights[n]) for n, st in used]),
                source_ts={n: st.source_ts for n, st in used},
                computed_at=datetime.now(timezone.utc),
                constituents_healthy=len(used),
                constituent_count=len(self.specs),
            )

    def realized_vol(self, window_s: int = DEFAULT_VOL_WINDOW_S,
                     mono: float | None = None) -> float | None:
        """Annualized population std of sqrt(dt)-normalized log returns over
        the trailing window of 1s-resampled composite mids. None (fail closed)
        until the window holds enough samples spanning >= MIN_VOL_COVERAGE of
        it. The sample floor scales with the window (a 60s window can only
        ever hold ~59 one-second samples) but never exceeds MIN_VOL_SAMPLES."""
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            pts = [(t, p) for t, p in self._samples if t >= mono - window_s]
        need = max(2, min(MIN_VOL_SAMPLES,
                          int(MIN_VOL_COVERAGE * window_s / SAMPLE_INTERVAL_S)))
        if len(pts) < need:
            return None
        if pts[-1][0] - pts[0][0] < MIN_VOL_COVERAGE * window_s:
            return None
        rets = []
        for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt <= 0:
                continue
            rets.append(math.log(p1 / p0) / math.sqrt(dt))
        if len(rets) < need - 1:
            return None
        return float(np.std(np.asarray(rets)) * math.sqrt(SECONDS_PER_YEAR))

    def recent_move_pct(self, window_s: float = VELOCITY_WINDOW_S,
                       mono: float | None = None) -> float | None:
        """Signed fractional change in the composite mid over the trailing
        window_s (e.g. 0.006 = spot up 0.6% in the last minute). None
        (fail-closed) if the feed has stalled or the sample buffer doesn't
        yet cover the full window — used to detect a fast-moving spot so
        sizing can lean smaller into a move that may be gapping past the
        model's assumptions rather than confirming a stable mispricing."""
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return None
        latest_t, latest_p = samples[-1]
        if mono - latest_t > SAMPLE_INTERVAL_S * 2:
            return None  # feed stalled — don't compute off a stale reading
        if samples[0][0] > mono - window_s:
            return None  # not enough history yet to cover the window
        baseline_p = next(p for t, p in samples if t >= mono - window_s)
        if baseline_p == 0:
            return None
        return (latest_p - baseline_p) / baseline_p

    def health(self, mono: float | None = None) -> FeedHealth:
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            constituents = []
            for name, st in self._state.items():
                age = (mono - st.last_tick_mono
                       if st.last_tick_mono is not None else None)
                constituents.append(ConstituentHealth(
                    name=name, connected=st.connected, last_tick_age_s=age,
                    healthy=st.connected and age is not None
                    and age <= STALE_CONSTITUENT_S))
        healthy_count = sum(1 for c in constituents if c.healthy)
        return FeedHealth(
            constituents=constituents,
            healthy_count=healthy_count,
            constituent_count=len(self.specs),
            composite_available=self.current_composite(mono=mono) is not None,
        )
