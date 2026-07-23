"""Manual smoke test: run the composite feed live for 60s, print one line/s.

Usage: ./.venv/bin/python scripts/smoke_price_feed.py [seconds]

Live network (5 exchange WS feeds); never run from pytest. Output is not
committed — the script is.
"""
import asyncio
import sys

from kalshi_bots.skills.crypto_price_feed import CryptoPriceFeed


async def main(seconds: int) -> None:
    feed = CryptoPriceFeed()
    await feed.start()
    try:
        for i in range(seconds):
            await asyncio.sleep(1)
            spot = feed.current_composite()
            h = feed.health()
            ages = " ".join(
                f"{c.name}={'--' if c.last_tick_age_s is None else f'{c.last_tick_age_s:.1f}s'}"
                f"{'' if c.healthy else '!'}"
                for c in h.constituents)
            vol60 = feed.realized_vol(window_s=60)
            vol900 = feed.realized_vol(window_s=900)
            fmt_vol = lambda v: "n/a" if v is None else f"{v:.1%}"
            if spot is None:
                print(f"[{i + 1:3d}s] composite=UNAVAILABLE "
                      f"({h.healthy_count}/{h.constituent_count} healthy) {ages}")
            else:
                print(f"[{i + 1:3d}s] mid={spot.mid:,.2f} "
                      f"bid={spot.bid:,.2f} ask={spot.ask:,.2f} "
                      f"({spot.constituents_healthy}/{spot.constituent_count}) "
                      f"vol60={fmt_vol(vol60)} vol900={fmt_vol(vol900)} | {ages}")
    finally:
        await feed.stop()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 60))
