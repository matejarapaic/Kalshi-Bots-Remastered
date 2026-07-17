"""FastAPI + WebSocket dashboard.

Data contract note (Category A, flagged): the master plan said to reuse the
old bot's data contract, but no old-bot code exists on disk — this is a fresh
minimal contract carrying the mandated extension fields on every event:
`sport`, `league`, `game_id`, `signal_type`.

Endpoints:
  GET /api/state  -> exposure summary, open trades, recent events
  WS  /ws         -> pushes each new orchestrator event as JSON

Requires the optional [serve] extra (fastapi, uvicorn). Import is lazy so the
core system runs without it.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json


def create_app(orchestrator):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect

    app = FastAPI(title="kalshi-bots dashboard")

    @app.get("/api/state")
    def state():
        exp = orchestrator.risk.exposure()
        return {
            "env": "demo",
            "exposure": dataclasses.asdict(exp),
            "open_trades": [
                {"sport": t["league"], "league": t["league"],
                 "game_id": t["espn_event_id"], "signal_type": None,
                 "skill": t["skill"], "market_ticker": t["market_ticker"],
                 "side": t["side"], "contracts": t["contracts"],
                 "entry_price_cents": t["entry_price"]}
                for t in orchestrator.trader.open_trades.values()
            ],
            "events": orchestrator.events[-100:],
        }

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        cursor = len(orchestrator.events)
        try:
            while True:
                if cursor < len(orchestrator.events):
                    for evt in orchestrator.events[cursor:]:
                        await websocket.send_text(json.dumps(evt))
                    cursor = len(orchestrator.events)
                await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            pass

    return app


def main():  # pragma: no cover - manual entrypoint
    import uvicorn

    from kalshi_bots.orchestrator import Orchestrator
    orch = Orchestrator(paper=True)
    uvicorn.run(create_app(orch), host="127.0.0.1", port=8800)


if __name__ == "__main__":  # pragma: no cover
    main()
