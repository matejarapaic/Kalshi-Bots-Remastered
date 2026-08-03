"""Manual smoke test: recent settled trades (Kalshi's History tab).

Usage: ./.venv/bin/python scripts/smoke_settlements.py [limit]

READ-ONLY — GET /portfolio/settlements only, no orders, no writes. Requires
credentials in .env. Prints the same columns the dashboard's Recent-trades
table shows, so the derived math (final position = net of both sides, total
cost = yes+no trade cost + fees, return = payout − cost) can be eyeballed
against Kalshi's own History tab. Output is not committed — the script is.
"""
import sys

from kalshi_bots.env import load_env
from kalshi_bots.skills.kalshi_client import KalshiClient, settled_trade_summary


def main(limit: int) -> None:
    load_env()
    kalshi = KalshiClient()
    print(f"env={kalshi.env()}  fetching {limit} recent settlements (read-only)\n")
    setts = kalshi.get_recent_settlements(limit=limit)
    hdr = (f"{'market':<28} {'outcome':<7} {'final pos':>12} "
           f"{'payout':>9} {'total cost':>11} {'return':>18}")
    print(hdr)
    print("-" * len(hdr))
    for s in setts:
        r = settled_trade_summary(s)
        pos = f"{r['position_count']:g} {r['position_side']}"
        ret = f"${r['return_cents']/100:+.2f} ({r['return_pct']:.0f}%)"
        print(f"{r['market_ticker']:<28} {r['outcome']:<7} {pos:>12} "
              f"${r['payout_cents']/100:>8.2f} ${r['total_cost_cents']/100:>10.2f} {ret:>18}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 15)
