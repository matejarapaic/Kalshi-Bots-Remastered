"""Manual smoke test: Kalshi market-data WebSocket against the demo env.

Usage: ./.venv/bin/python scripts/smoke_kalshi_ws.py [seconds]

Requires demo credentials in .env (KALSHI_KEY_ID / KALSHI_KEY_PATH). Resolves
the currently-active KXBTC15M window via REST, subscribes its order book and
the BRTI index stream, prints one status line per second. Verifies live: the
WS auth handshake, subscribe shapes, snapshot/delta application, seq handling,
and the cfbenchmarks_value message shape. Output is not committed — the
script is.
"""
import asyncio
import sys
from datetime import datetime, timezone

from kalshi_bots.env import load_env
from kalshi_bots.skills.kalshi_client import KalshiClient
from kalshi_bots.skills.kalshi_ws_orderbook import KalshiOrderBook
from kalshi_bots.skills.window_monitor import WindowResolver
from kalshi_bots.types import MarketRef


async def main(seconds: int) -> None:
    load_env()
    kalshi = KalshiClient()
    resolver = WindowResolver(kalshi)
    w = resolver.resolve_active(datetime.now(timezone.utc))
    if w is None:
        print("no resolvable active window — aborting")
        return
    print(f"active window: {w.market_ticker} strike={w.strike} "
          f"closes {w.closes_at.isoformat()}")
    market = MarketRef(family="crypto", series_ticker=w.series_ticker,
                       event_ticker=w.event_ticker,
                       market_ticker=w.market_ticker, yes_label="up", title="",
                       close_ts=w.closes_at, settlement_notes=None)
    book = KalshiOrderBook(kalshi)
    await book.start()
    await book.subscribe(market)
    try:
        for i in range(seconds):
            await asyncio.sleep(1)
            snap = book.snapshot(w.market_ticker)
            h = book.health(w.market_ticker)
            b = book.brti()
            if snap is None:
                print(f"[{i + 1:3d}s] book=NONE connected={h.connected} "
                      f"subscribed={h.subscribed} gap={h.seq_gap}")
            else:
                print(f"[{i + 1:3d}s] yes {snap.yes_bid}/{snap.yes_ask} "
                      f"no {snap.no_bid}/{snap.no_ask} spread={snap.spread_cents} "
                      f"devig={snap.devigged_yes_prob and round(snap.devigged_yes_prob, 3)} "
                      f"| brti={b.value if b else None} "
                      f"avg60={b.avg_60s if b else None} "
                      f"settle_forming={b.settlement_forming if b else None}")
    finally:
        await book.stop()
        if book.brti() is not None:
            print("\nlast raw BRTI frame (for parser verification):")
            print(book.brti().raw)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
