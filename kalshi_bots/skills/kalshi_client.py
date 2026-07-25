"""kalshi-client skill. Spec: skills/kalshi-client/SKILL.md.

The only component that talks to the Kalshi API. Orderbook truth only; derived
asks and de-vig live here and nowhere else. Auth ported from AnitaKirkovska/
kalshi-cli (RSA-PSS over timestamp+METHOD+path, no query string).
"""
from __future__ import annotations

import base64
import math
import os
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_bots.types import (
    Cents, DepthLevel, Fill, MarketRef, OrderbookSnapshot, OrderRequest,
    OrderResult, Position, Settlement, Side,
)

DEFAULT_HOST_DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"
DEFAULT_HOST_PROD = "https://external-api.kalshi.com/trade-api/v2"
FEE_RATE = 0.07  # named so a schedule change is one edit
TRADEABLE_STATUSES = {"active", "open"}


class KalshiClientError(Exception):
    pass


class KalshiAuthError(KalshiClientError):
    pass


class KalshiNotFound(KalshiClientError):
    pass


class KalshiRateLimitError(KalshiClientError):
    pass


class KalshiOrderRejected(KalshiClientError):
    pass


class KalshiProdRefused(KalshiClientError):
    pass


def dollars_to_cents(s: str | float) -> Cents:
    """'0.4400' -> 44. Boundary conversion; nothing outside sees dollar-strings."""
    return int(round(Decimal(str(s)) * 100))


def est_fee_cents(contracts: int, price: Cents) -> int:
    """ceil(0.07 * contracts * p * (1-p)) in cents, integer math, entry-only."""
    if contracts <= 0:
        return 0
    return math.ceil(7 * contracts * price * (100 - price) / 10000)


def depth_within(snapshot: OrderbookSnapshot, side: Side, cents_from_best: int) -> int:
    """Contracts available in `side`'s ask ladder within cents_from_best of best ask."""
    book = snapshot.yes_book if side == "yes" else snapshot.no_book
    if not book:
        return 0
    best = book[0].price
    return sum(l.quantity for l in book if l.price <= best + cents_from_best)


def _parse_book_side(levels: list) -> list[DepthLevel]:
    """[[price_dollar_str, qty_str], ...] -> DepthLevel list, qty floored to int."""
    out = []
    for price_s, qty_s in levels or []:
        qty = int(float(qty_s))  # never round available size up
        if qty > 0:
            out.append(DepthLevel(price=dollars_to_cents(price_s), quantity=qty))
    return out


def build_snapshot(market: MarketRef, orderbook_raw: dict,
                   fetched_at: datetime | None = None) -> OrderbookSnapshot:
    """Pure function: raw orderbook_fp payload -> OrderbookSnapshot.

    Each side of orderbook_fp lists BIDS only. Derived asks (spec rule 6):
    the YES ask ladder is the NO bid ladder mirrored (price -> 100 - price).
    """
    fetched_at = fetched_at or datetime.now(timezone.utc)
    ob = orderbook_raw.get("orderbook_fp") or orderbook_raw.get("orderbook") or {}
    yes_bids = sorted(_parse_book_side(ob.get("yes_dollars") or ob.get("yes")),
                      key=lambda l: -l.price)
    no_bids = sorted(_parse_book_side(ob.get("no_dollars") or ob.get("no")),
                     key=lambda l: -l.price)

    yes_bid = yes_bids[0].price if yes_bids else None
    no_bid = no_bids[0].price if no_bids else None
    yes_ask = (100 - no_bid) if no_bid is not None else None
    no_ask = (100 - yes_bid) if yes_bid is not None else None

    yes_book = sorted((DepthLevel(100 - l.price, l.quantity) for l in no_bids),
                      key=lambda l: l.price)
    no_book = sorted((DepthLevel(100 - l.price, l.quantity) for l in yes_bids),
                     key=lambda l: l.price)

    devigged = None
    spread = None
    if yes_bid is not None and no_bid is not None:
        yes_mid = (yes_bid + yes_ask) / 2
        no_mid = (no_bid + no_ask) / 2
        devigged = yes_mid / (yes_mid + no_mid)
        spread = yes_ask - yes_bid  # may be negative on a transient crossed book

    return OrderbookSnapshot(
        market=market, yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid,
        no_ask=no_ask, yes_book=yes_book, no_book=no_book,
        devigged_yes_prob=devigged, spread_cents=spread, fetched_at=fetched_at,
    )


def _parse_market(raw: dict, family: str = "") -> MarketRef:
    close_ts = None
    for key in ("expected_expiration_time", "close_time"):
        if raw.get(key):
            close_ts = datetime.fromisoformat(raw[key].replace("Z", "+00:00"))
            break
    ticker = raw.get("ticker", "")
    event_ticker = raw.get("event_ticker", "")
    suffix = ticker[len(event_ticker) + 1:] if ticker.startswith(event_ticker + "-") else ""
    series = event_ticker.split("-")[0] if event_ticker else ""
    return MarketRef(
        family=family, series_ticker=series, event_ticker=event_ticker,
        market_ticker=ticker,
        yes_label=raw.get("yes_sub_title") or suffix,
        title=raw.get("title", ""),
        close_ts=close_ts, settlement_notes=raw.get("rules_secondary"),
    )


class _RateLimiter:
    """Token bucket: rps tokens/s, burst capacity."""

    def __init__(self, rps: float, burst: int):
        self.rps, self.burst = rps, burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rps)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            wait = (1 - self.tokens) / self.rps
        time.sleep(wait)
        with self.lock:
            self.tokens = max(0.0, self.tokens - 1)


class KalshiClient:
    def __init__(self, session: requests.Session | None = None):
        self._env = os.environ.get("KALSHI_ENV", "demo")
        if self._env not in ("demo", "prod"):
            raise KalshiClientError(f"KALSHI_ENV must be demo|prod, got {self._env!r}")
        if self._env == "prod" and os.environ.get("KALSHI_ALLOW_PROD") != "yes-i-mean-it":
            raise KalshiProdRefused(
                "KALSHI_ENV=prod refused: set KALSHI_ALLOW_PROD=yes-i-mean-it "
                "only after the Phase 3 final checkpoint re-confirms Category B items."
            )
        self.host = (os.environ.get("KALSHI_HOST_PROD", DEFAULT_HOST_PROD)
                     if self._env == "prod"
                     else os.environ.get("KALSHI_HOST_DEMO", DEFAULT_HOST_DEMO))
        self._root_prefix = "/" + self.host.split("/", 3)[3]  # /trade-api/v2
        self.session = session or requests.Session()
        self.limiter = _RateLimiter(float(os.environ.get("KALSHI_RPS", "5")), 10)
        self._key_id = os.environ.get("KALSHI_KEY_ID")
        self._private_key = None
        key_path = os.environ.get("KALSHI_KEY_PATH")
        if key_path:
            with open(os.path.expanduser(key_path), "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def env(self) -> str:
        return self._env

    # --- auth (ported from kalshi.js) ---

    def _sign(self, ts_ms: str, method: str, sign_path: str) -> str:
        msg = f"{ts_ms}{method}{sign_path}".encode()
        sig = self._private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str) -> dict:
        if not (self._key_id and self._private_key):
            return {}
        ts = str(int(time.time() * 1000))
        sign_path = self._root_prefix + path.split("?", 1)[0]  # no query string
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, sign_path),
        }

    def ws_auth_headers(self, ws_path: str = "/trade-api/ws/v2") -> dict:
        """Connect-time headers for the market-data WebSocket: the same
        RSA-PSS scheme signed over `timestamp + "GET" + ws_path` (verified
        against docs.kalshi.com quick-start, 2026-07-22). The WS connection
        itself always requires auth — there is no public channel."""
        if not (self._key_id and self._private_key):
            raise KalshiAuthError("KALSHI_KEY_ID/KALSHI_KEY_PATH required for WS")
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, "GET", ws_path),
        }

    def _req(self, method: str, path: str, body: dict | None = None,
             auth_required: bool = False) -> dict:
        if auth_required and not (self._key_id and self._private_key):
            raise KalshiAuthError("KALSHI_KEY_ID/KALSHI_KEY_PATH required for this call")
        backoff = 1.0
        for attempt in range(4):
            self.limiter.acquire()
            resp = self.session.request(
                method, self.host + path, json=body,
                headers=self._headers(method, path), timeout=15,
            )
            if resp.status_code == 404:
                raise KalshiNotFound(path)
            if resp.status_code in (401, 403):
                raise KalshiAuthError(f"{resp.status_code}: {resp.text[:200]}")
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 3:
                    raise (KalshiRateLimitError if resp.status_code == 429
                           else KalshiClientError)(f"{resp.status_code} after retries")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code >= 400:
                raise KalshiClientError(f"{resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise KalshiClientError("unreachable")

    # --- market data ---

    def get_market(self, market_ticker: str, family: str = "") -> MarketRef:
        raw = self._req("GET", f"/markets/{market_ticker}")
        return _parse_market(raw.get("market", raw), family)

    def get_market_raw(self, market_ticker: str) -> dict:
        """Full market object verbatim — for fields MarketRef doesn't carry
        (floor_strike, status, open/close times). window-monitor's
        verification path; never used for pricing (orderbook truth only)."""
        raw = self._req("GET", f"/markets/{market_ticker}")
        return raw.get("market", raw)

    def get_markets(self, series_ticker: str, status: str | None = "open",
                    family: str = "") -> list[MarketRef]:
        out, cursor = [], None
        while True:
            path = f"/markets?series_ticker={series_ticker}&limit=200"
            if status:
                path += f"&status={status}"
            if cursor:
                path += f"&cursor={cursor}"
            raw = self._req("GET", path)
            out.extend(_parse_market(m, family) for m in raw.get("markets", []))
            cursor = raw.get("cursor")
            if not cursor or not raw.get("markets"):
                return out

    def get_orderbook(self, market: MarketRef) -> OrderbookSnapshot:
        raw = self._req("GET", f"/markets/{market.market_ticker}/orderbook")
        return build_snapshot(market, raw)

    # --- portfolio ---

    def get_balance(self) -> int:
        raw = self._req("GET", "/portfolio/balance", auth_required=True)
        # balance in cents per API; tolerate dollar-string migration
        if "balance" in raw:
            return int(raw["balance"])
        return dollars_to_cents(raw["balance_dollars"])

    def get_positions(self) -> list[Position]:
        raw = self._req("GET", "/portfolio/positions", auth_required=True)
        out = []
        for p in raw.get("market_positions", []):
            qty = int(p.get("position", 0))
            if qty == 0:
                continue
            side: Side = "yes" if qty > 0 else "no"
            exposure = p.get("market_exposure_dollars")
            avg = (dollars_to_cents(Decimal(str(exposure)) / abs(qty))
                   if exposure is not None else 0)
            fees = dollars_to_cents(p.get("fees_paid_dollars", "0"))
            out.append(Position(market_ticker=p["ticker"], side=side,
                                contracts=abs(qty), avg_price=avg,
                                fees_paid_cents=abs(fees), raw=p))
        return out

    def get_fills(self, market_ticker: str | None = None) -> list[Fill]:
        path = "/portfolio/fills?limit=200"
        if market_ticker:
            path += f"&ticker={market_ticker}"
        raw = self._req("GET", path, auth_required=True)
        out = []
        for f in raw.get("fills", []):
            price_key = "yes_price_dollars" if f.get("side") == "yes" else "no_price_dollars"
            price = dollars_to_cents(f.get(price_key, f.get("price_dollars", "0")))
            out.append(Fill(
                order_id=f.get("order_id", ""), market_ticker=f.get("ticker", ""),
                side=f.get("side", "yes"), action=f.get("action", "buy"),
                contracts=int(float(f.get("count_fp", f.get("count", 0)))),
                price=price,
                # Live fill payloads carry the fee as `fee_cost` (dollar string,
                # sibling to yes/no_price_dollars) — NOT `taker_fees_dollars`,
                # which only exists on the *order* object (_order_result). Reading
                # the wrong key silently left taker_fee_cents=0, dropping real fees
                # from the ledger's cost basis and P&L (verified live 2026-07-24;
                # same class as the settlement revenue field fix). Fallback to the
                # old key kept purely for safety.
                taker_fee_cents=abs(dollars_to_cents(
                    f.get("fee_cost", f.get("taker_fees_dollars", "0")) or "0")),
                ts=datetime.fromisoformat(f["created_time"].replace("Z", "+00:00"))
                if f.get("created_time") else datetime.now(timezone.utc),
                raw=f,
            ))
        return out

    def get_settlements(self, market_ticker: str) -> list[Settlement]:
        raw = self._req("GET", f"/portfolio/settlements?ticker={market_ticker}",
                        auth_required=True)
        out = []
        for s in raw.get("settlements", []):
            result = s.get("market_result", "")
            out.append(Settlement(
                market_ticker=s.get("ticker", market_ticker),
                result=result if result in ("yes", "no") else "void",
                settled_ts=datetime.fromisoformat(s["settled_time"].replace("Z", "+00:00"))
                if s.get("settled_time") else None,
                revenue_cents=dollars_to_cents(s.get("revenue_dollars", "0") or "0"),
                raw=s,
            ))
        return out

    # --- orders ---

    def place_order(self, req: OrderRequest) -> OrderResult:
        if req.action == "buy" and req.limit_price is None:
            raise KalshiOrderRejected("market orders forbidden on entry (spec rule 11)")
        # v2 book is YES-only: "bid" buys YES, "ask" sells YES (economically
        # equivalent to selling/buying NO at 1 - price) — see create-order-v2
        # BookSide spec. book_side depends on BOTH side and action: buying NO
        # is a "sell YES" (ask), but *selling* NO (an exit) is a "buy YES"
        # (bid) — collapsing this to side-only previously sent every NO exit
        # as another ask, i.e. another NO buy that doubled the position
        # instead of closing it. Found live 2026-07-24 after a real fill
        # confirmed a "sell no" exit executed as a second no buy at 1c.
        if req.side == "yes":
            book_side = "bid" if req.action == "buy" else "ask"
            price_cents = req.limit_price
        else:
            book_side = "ask" if req.action == "buy" else "bid"
            price_cents = 100 - req.limit_price
        body = {
            "ticker": req.market_ticker, "side": book_side,
            "count": f"{req.contracts:.2f}",
            "price": f"{price_cents / 100:.4f}",
            "type": "limit",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": req.client_order_id,
        }
        try:
            raw = self._req("POST", "/portfolio/events/orders", body=body, auth_required=True)
        except KalshiClientError as e:
            if isinstance(e, (KalshiAuthError, KalshiRateLimitError)):
                raise
            raise KalshiOrderRejected(str(e)) from e
        return self._order_result(raw.get("order", raw))

    def cancel_order(self, order_id: str) -> OrderResult:
        raw = self._req("DELETE", f"/portfolio/orders/{order_id}", auth_required=True)
        return self._order_result(raw.get("order", raw))

    @staticmethod
    def _order_result(o: dict) -> OrderResult:
        # v2 has two incompatible order-shaped responses, both verified live
        # 2026-07-18: POST create-order returns a flat {fill_count,
        # average_fill_price, average_fee_paid, remaining_count, order_id}
        # with NO "status" field; GET /portfolio/orders/{id} returns
        # {"order": {fill_count_fp, taker_fill_cost_dollars,
        # maker_fill_cost_dollars, taker_fees_dollars, maker_fees_dollars,
        # status, ...}}. Handling only the GET shape (an earlier attempt at
        # this fix) still silently computed filled_contracts=0 for every
        # real fill via the POST path — the one place_order actually uses.
        status_map = {"resting": "resting", "executed": "filled", "canceled": "canceled",
                      "pending": "resting"}
        filled = int(float(o.get("fill_count_fp", o.get("fill_count", 0)) or 0))

        if "average_fill_price" in o:
            avg = dollars_to_cents(o["average_fill_price"]) if filled else None
            fee_total = Decimal(str(o.get("average_fee_paid", "0") or "0")) * filled
        else:
            cost = (Decimal(str(o.get("taker_fill_cost_dollars", "0") or "0"))
                    + Decimal(str(o.get("maker_fill_cost_dollars", "0") or "0")))
            avg = dollars_to_cents(cost / filled) if filled else None
            fee_total = (Decimal(str(o.get("taker_fees_dollars", "0") or "0"))
                         + Decimal(str(o.get("maker_fees_dollars", "0") or "0")))

        if "status" in o:
            status = status_map.get(o["status"], "rejected")
        else:
            remaining = float(o.get("remaining_count", o.get("remaining_count_fp", 0)) or 0)
            status = "filled" if filled else ("canceled" if remaining == 0 else "resting")

        return OrderResult(
            order_id=o.get("order_id", ""),
            status=status, filled_contracts=filled, avg_fill_price=avg,
            fee_cents=abs(dollars_to_cents(fee_total)),
            raw=o,
        )
