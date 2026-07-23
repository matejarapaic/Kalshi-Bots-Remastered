"""kalshi-ws-orderbook skill. Spec: skills/kalshi-ws-orderbook/SKILL.md.

Streaming order books for the active crypto contract(s) over Kalshi's
market-data WebSocket (api_verified: 2026-07-22 against docs.kalshi.com
asyncapi.yaml + live endpoint probes), plus the `cfbenchmarks_value` channel
streaming BRTI — the settlement index itself, including the settlement-window
average as it forms in the final minute.

Wire facts this module encodes:
- Auth is required for the connection itself (no public channel); the headers
  come from kalshi-client's `ws_auth_headers()` — same RSA-PSS scheme as REST.
- Prices arrive as dollar strings (`price_dollars`, `yes_dollars_fp` pairs)
  with sub-cent ticks in the tails (`tapered_deci_cent`); sizes are
  fixed-point strings and may be fractional. Books are one-sided bids per
  side, exactly like REST — snapshots are built through kalshi-client's
  `build_snapshot`, so derived-ask/de-vig math stays in one place. Sub-cent
  levels aggregate into whole-cent buckets (documented approximation; sizes
  floor, never round up).
- `seq` must be contiguous per subscription; a gap marks the book unhealthy
  and triggers a re-snapshot (fail closed until it arrives).

Memory bounds (24/7 hygiene): one dict of price->qty per side per subscribed
ticker (only the active window ± one is ever subscribed), last BRTI tick only.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshi_bots.skills.kalshi_client import KalshiClient, dollars_to_cents
from kalshi_bots.types import BookHealth, BrtiState, MarketRef, OrderbookSnapshot

log = logging.getLogger(__name__)

DEFAULT_WS_DEMO = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
DEFAULT_WS_PROD = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
STALE_BOOK_S = 5.0             # no update in this long -> unhealthy
RECONNECT_MAX_BACKOFF_S = 30.0
BRTI_INDEX_ID = "BRTI"


class KalshiWsError(Exception):
    pass


@dataclass
class _BookState:
    market: MarketRef
    yes: dict[int, float] = field(default_factory=dict)   # cents -> contracts
    no: dict[int, float] = field(default_factory=dict)
    sid: int | None = None
    last_seq: int | None = None
    seq_gap: bool = False
    have_snapshot: bool = False
    last_update_mono: float | None = None


def _parse_side(levels: list | None) -> dict[int, float]:
    out: dict[int, float] = {}
    for price_s, qty_s in levels or []:
        try:
            cents, qty = dollars_to_cents(price_s), float(qty_s)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            out[cents] = out.get(cents, 0.0) + qty
    return out


def _first_float(d: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


class KalshiOrderBook:
    """Reads are thread-safe; all writes happen on the asyncio loop that ran
    start(). Subscribe/unsubscribe are async because they talk to the socket;
    the desired-subscription set survives reconnects."""

    def __init__(self, kalshi_client: KalshiClient, ws_url: str | None = None,
                 want_brti: bool = True):
        import os
        self.kalshi = kalshi_client
        env = kalshi_client.env()
        self.ws_url = ws_url or (
            os.environ.get("KALSHI_WS_HOST_PROD", DEFAULT_WS_PROD) if env == "prod"
            else os.environ.get("KALSHI_WS_HOST_DEMO", DEFAULT_WS_DEMO))
        self.want_brti = want_brti
        self._books: dict[str, _BookState] = {}
        self._brti: BrtiState | None = None
        self._brti_sid: int | None = None
        self._connected = False
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._ws = None
        self._stopping = False
        self._cmd_id = 0
        self._pending: dict[int, tuple[str, str | None]] = {}  # id -> (channel, ticker)

    # --- lifecycle ---

    async def start(self) -> None:
        if self._task is not None:
            raise KalshiWsError("orderbook client already started")
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="kalshi-ws")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        import orjson
        import websockets
        backoff = 1.0
        while not self._stopping:
            try:
                headers = self.kalshi.ws_auth_headers(WS_PATH)
                async with websockets.connect(self.ws_url, open_timeout=10,
                                              close_timeout=5,
                                              additional_headers=headers) as ws:
                    self._ws = ws
                    with self._lock:
                        self._connected = True
                    backoff = 1.0
                    await self._resubscribe_all()
                    async for raw in ws:
                        self._handle_message(orjson.loads(raw))
            except asyncio.CancelledError:
                self._mark_disconnected()
                raise
            except Exception as e:
                log.warning("kalshi ws dropped: %s", e)
            self._mark_disconnected()
            if self._stopping:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF_S)

    def _mark_disconnected(self) -> None:
        self._ws = None
        with self._lock:
            self._connected = False
            for st in self._books.values():
                st.sid = None
                st.have_snapshot = False
                st.last_seq = None
            self._brti_sid = None

    async def _resubscribe_all(self) -> None:
        for ticker in list(self._books):
            await self._send_subscribe(ticker)
        if self.want_brti:
            await self._send({"cmd": "subscribe",
                              "params": {"channels": ["cfbenchmarks_value"],
                                         "index_ids": [BRTI_INDEX_ID]}},
                             channel="cfbenchmarks_value")

    # --- commands ---

    async def _send(self, cmd: dict, channel: str, ticker: str | None = None) -> None:
        import orjson
        if self._ws is None:
            return  # desired state is applied on (re)connect
        self._cmd_id += 1
        cmd = {"id": self._cmd_id, **cmd}
        self._pending[self._cmd_id] = (channel, ticker)
        await self._ws.send(orjson.dumps(cmd).decode())

    async def _send_subscribe(self, ticker: str) -> None:
        await self._send({"cmd": "subscribe",
                          "params": {"channels": ["orderbook_delta"],
                                     "market_ticker": ticker}},
                         channel="orderbook_delta", ticker=ticker)

    async def subscribe(self, market: MarketRef) -> None:
        ticker = market.market_ticker
        with self._lock:
            if ticker in self._books:
                return
            self._books[ticker] = _BookState(market=market)
        await self._send_subscribe(ticker)

    async def unsubscribe(self, market_ticker: str) -> None:
        with self._lock:
            st = self._books.pop(market_ticker, None)
        if st is not None and st.sid is not None:
            await self._send({"cmd": "unsubscribe", "params": {"sids": [st.sid]}},
                             channel="unsubscribe")

    async def _request_resnapshot(self, st: _BookState) -> None:
        if st.sid is not None:
            await self._send({"cmd": "update_subscription",
                              "params": {"sids": [st.sid], "action": "get_snapshot"}},
                             channel="orderbook_delta",
                             ticker=st.market.market_ticker)
        else:  # never got a subscribed ack: full resubscribe
            await self._send_subscribe(st.market.market_ticker)

    # --- message dispatch (tests drive this directly) ---

    def _handle_message(self, m: dict, mono: float | None = None) -> None:
        mono = time.monotonic() if mono is None else mono
        mtype = m.get("type")
        if mtype == "orderbook_snapshot":
            self._on_snapshot(m, mono)
        elif mtype == "orderbook_delta":
            self._on_delta(m, mono)
        elif mtype == "cfbenchmarks_value":
            self._on_brti(m)
        elif mtype == "subscribed":
            self._on_subscribed(m)
        elif mtype == "error":
            code = (m.get("msg") or {}).get("code")
            log.error("kalshi ws error (cmd id=%s code=%s): %s",
                      m.get("id"), code, m.get("msg"))
            self._pending.pop(m.get("id"), None)

    def _on_subscribed(self, m: dict) -> None:
        body = m.get("msg") or {}
        sid = body.get("sid", m.get("sid"))
        channel, ticker = self._pending.pop(m.get("id"), (body.get("channel"), None))
        if channel == "cfbenchmarks_value":
            self._brti_sid = sid
            return
        with self._lock:
            if ticker is not None and ticker in self._books:
                self._books[ticker].sid = sid

    def _on_snapshot(self, m: dict, mono: float) -> None:
        body = m.get("msg") or {}
        ticker = body.get("market_ticker", "")
        with self._lock:
            st = self._books.get(ticker)
            if st is None:
                return  # late message for an unsubscribed window
            st.yes = _parse_side(body.get("yes_dollars_fp") or body.get("yes"))
            st.no = _parse_side(body.get("no_dollars_fp") or body.get("no"))
            if m.get("sid") is not None:
                st.sid = m["sid"]
            st.last_seq = m.get("seq")
            st.seq_gap = False
            st.have_snapshot = True
            st.last_update_mono = mono

    def _on_delta(self, m: dict, mono: float) -> None:
        body = m.get("msg") or {}
        ticker = body.get("market_ticker", "")
        gap_st = None
        with self._lock:
            st = self._books.get(ticker)
            if st is None:
                return
            seq = m.get("seq")
            if (st.last_seq is not None and seq is not None
                    and seq != st.last_seq + 1) or not st.have_snapshot:
                # lost a message (or never had the base): the book is no
                # longer trustworthy until a fresh snapshot replaces it
                st.seq_gap = True
                gap_st = st
            else:
                st.last_seq = seq if seq is not None else st.last_seq
                try:
                    cents = dollars_to_cents(body["price_dollars"])
                    delta = float(body["delta_fp"])
                    side = body.get("side")
                except (KeyError, TypeError, ValueError):
                    log.warning("malformed delta for %s: %r", ticker, body)
                    return
                book = st.yes if side == "yes" else st.no
                q = book.get(cents, 0.0) + delta
                if q > 1e-9:
                    book[cents] = q
                else:
                    book.pop(cents, None)
                st.last_update_mono = mono
        if gap_st is not None:
            log.warning("seq gap on %s — requesting re-snapshot", ticker)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._request_resnapshot(gap_st))
            except RuntimeError:
                pass  # no loop (offline tests): gap flag alone fails closed

    def _on_brti(self, m: dict) -> None:
        body = m.get("msg") or {}
        if body.get("index_id") not in (BRTI_INDEX_ID, None):
            return
        data = body.get("data") or {}
        ts = None
        ts_ms = body.get("ts_ms") or data.get("ts_ms")
        if ts_ms:
            try:
                ts = datetime.fromtimestamp(int(ts_ms) / 1e3, tz=timezone.utc)
            except (TypeError, ValueError):
                ts = None
        state = BrtiState(
            value=_first_float(data, ("value", "price", "index_value", "v")),
            avg_60s=_first_float(body, ("avg_60s_data",))
            if not isinstance(body.get("avg_60s_data"), dict)
            else _first_float(body["avg_60s_data"], ("value", "price", "v")),
            settlement_forming=_first_float(
                body, ("last_60s_windowed_average_15min",))
            if not isinstance(body.get("last_60s_windowed_average_15min"), dict)
            else _first_float(body["last_60s_windowed_average_15min"],
                              ("value", "price", "v")),
            ts=ts, fetched_at=datetime.now(timezone.utc), raw=body,
        )
        with self._lock:
            self._brti = state

    # --- reads ---

    def snapshot(self, market_ticker: str) -> OrderbookSnapshot | None:
        """None until a snapshot has arrived, and again whenever a seq gap is
        outstanding — a book we can't trust is a book we don't have."""
        from kalshi_bots.skills.kalshi_client import build_snapshot
        with self._lock:
            st = self._books.get(market_ticker)
            if st is None or not st.have_snapshot or st.seq_gap:
                return None
            raw = {"orderbook_fp": {
                "yes_dollars": [[f"{c / 100:.4f}", str(q)]
                                for c, q in sorted(st.yes.items())],
                "no_dollars": [[f"{c / 100:.4f}", str(q)]
                               for c, q in sorted(st.no.items())],
            }}
            market = st.market
        return build_snapshot(market, raw)

    def health(self, market_ticker: str, mono: float | None = None) -> BookHealth:
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            st = self._books.get(market_ticker)
            connected = self._connected
            if st is None:
                return BookHealth(market_ticker=market_ticker, connected=connected,
                                  subscribed=False, last_update_age_s=None,
                                  seq_gap=False, healthy=False)
            age = (mono - st.last_update_mono
                   if st.last_update_mono is not None else None)
            healthy = (connected and st.have_snapshot and not st.seq_gap
                       and age is not None and age <= STALE_BOOK_S)
            return BookHealth(market_ticker=market_ticker, connected=connected,
                              subscribed=True, last_update_age_s=age,
                              seq_gap=st.seq_gap, healthy=healthy)

    def brti(self) -> BrtiState | None:
        with self._lock:
            return self._brti
