"""Paper-execution broker: simulates fills against live public orderbooks.

Category A implementation choice (flagged in the build log): the master plan
requires a full paper cycle before prod, but portfolio endpoints need API
keys the build environment doesn't hold. PaperBroker proxies *market data*
to the real (public) Kalshi API and simulates *portfolio* state locally, so
a complete demo cycle can run end to end. When demo API keys are provided,
the real KalshiClient replaces this transparently (same interface).
"""
from __future__ import annotations

import itertools
from datetime import datetime, timezone

from kalshi_bots.skills.kalshi_client import est_fee_cents
from kalshi_bots.types import (
    Fill, MarketRef, OrderRequest, OrderResult, Position, Settlement,
)


class PaperBroker:
    def __init__(self, kalshi_client, starting_balance_cents: int = 50_000):
        self.kalshi = kalshi_client
        self.cash = starting_balance_cents
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.settled: dict[str, Settlement] = {}
        self._ids = itertools.count(1)

    def env(self) -> str:
        return "demo"  # paper is a demo-mode simulation

    # --- market data: proxied to the real public API ---

    def get_market(self, market_ticker: str, league: str = "") -> MarketRef:
        return self.kalshi.get_market(market_ticker, league)

    def get_markets(self, series_ticker: str, status: str | None = "open",
                    league: str = "") -> list[MarketRef]:
        return self.kalshi.get_markets(series_ticker, status, league)

    def get_orderbook(self, market):
        return self.kalshi.get_orderbook(market)

    # --- portfolio: simulated ---

    def get_balance(self) -> int:
        return self.cash

    def get_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.contracts > 0]

    def get_fills(self, market_ticker: str | None = None) -> list[Fill]:
        return [f for f in self.fills
                if market_ticker is None or f.market_ticker == market_ticker]

    def get_settlements(self, market_ticker: str) -> list[Settlement]:
        s = self.settled.get(market_ticker)
        return [s] if s else []

    def settle(self, market_ticker: str, result: str) -> None:
        """Paper settlement (driven by the analyst from ESPN finals)."""
        pos = self.positions.get(market_ticker)
        revenue = 0
        if pos and pos.contracts > 0:
            won = pos.side == result
            revenue = pos.contracts * 100 if won else 0
            self.cash += revenue
            pos.contracts = 0
        self.settled[market_ticker] = Settlement(
            market_ticker=market_ticker,
            result=result if result in ("yes", "no") else "void",
            settled_ts=datetime.now(timezone.utc), revenue_cents=revenue, raw={})

    def place_order(self, req: OrderRequest, snapshot=None) -> OrderResult:
        """Fill against the current (live) book: a buy fills up to the depth
        available at or under the limit price; remainder is canceled (IOC)."""
        market = self.kalshi.get_market(req.market_ticker)
        ob = snapshot or self.kalshi.get_orderbook(market)
        book = ob.yes_book if req.side == "yes" else ob.no_book
        oid = f"paper-{next(self._ids)}"

        if req.action == "buy":
            filled, cost = 0, 0
            for level in book:
                if level.price > req.limit_price or filled >= req.contracts:
                    break
                take = min(level.quantity, req.contracts - filled)
                filled += take
                cost += take * level.price
            if filled == 0:
                return OrderResult(order_id=oid, status="canceled",
                                   filled_contracts=0, avg_fill_price=None,
                                   fee_cents=0, raw={"paper": True})
            avg = round(cost / filled)
            fee = est_fee_cents(filled, avg)
            self.cash -= cost + fee
            pos = self.positions.get(req.market_ticker)
            if pos:
                pos.contracts += filled
                pos.fees_paid_cents += fee
            else:
                self.positions[req.market_ticker] = Position(
                    market_ticker=req.market_ticker, side=req.side,
                    contracts=filled, avg_price=avg, fees_paid_cents=fee, raw={})
            fill = Fill(order_id=oid, market_ticker=req.market_ticker,
                        side=req.side, action="buy", contracts=filled, price=avg,
                        taker_fee_cents=fee, ts=datetime.now(timezone.utc),
                        raw={"paper": True})
            self.fills.append(fill)
            return OrderResult(order_id=oid, status="filled" if filled == req.contracts
                               else "partial", filled_contracts=filled,
                               avg_fill_price=avg, fee_cents=fee, raw={"paper": True})

        # sell: exit against the bid side of our contract type
        pos = self.positions.get(req.market_ticker)
        if not pos or pos.contracts == 0:
            return OrderResult(order_id=oid, status="rejected", filled_contracts=0,
                               avg_fill_price=None, fee_cents=0,
                               raw={"paper": True, "reason": "no position"})
        bid = ob.yes_bid if req.side == "yes" else ob.no_bid
        if bid is None or bid < req.limit_price:
            return OrderResult(order_id=oid, status="canceled", filled_contracts=0,
                               avg_fill_price=None, fee_cents=0, raw={"paper": True})
        qty = min(req.contracts, pos.contracts)
        fee = est_fee_cents(qty, bid)
        self.cash += qty * bid - fee
        pos.contracts -= qty
        fill = Fill(order_id=oid, market_ticker=req.market_ticker, side=req.side,
                    action="sell", contracts=qty, price=bid, taker_fee_cents=fee,
                    ts=datetime.now(timezone.utc), raw={"paper": True})
        self.fills.append(fill)
        return OrderResult(order_id=oid, status="filled", filled_contracts=qty,
                           avg_fill_price=bid, fee_cents=fee, raw={"paper": True})

    def cancel_order(self, order_id: str) -> OrderResult:
        return OrderResult(order_id=order_id, status="canceled", filled_contracts=0,
                           avg_fill_price=None, fee_cents=0, raw={"paper": True})
