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

NOTE: deliberately NO `from __future__ import annotations` here — postponed
annotations turn `websocket: WebSocket` into a string FastAPI cannot resolve
against module globals (WebSocket is imported lazily inside create_app), which
silently downgrades the param to a required query field and 403s every
handshake. Found live 2026-07-17.
"""
import asyncio
import dataclasses
import json
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "dashboard_static"


def create_app(orchestrator):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse

    app = FastAPI(title="kalshi-bots dashboard")

    @app.get("/")
    def index():
        # Presentation only: static page consuming /api/state + /ws unchanged.
        return FileResponse(STATIC_DIR / "index.html")

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
    import threading

    import uvicorn

    from kalshi_bots.orchestrator import Orchestrator
    orch = Orchestrator()  # auto-detects real demo execution vs paper
    threading.Thread(target=orch.run, daemon=True,
                     name="orchestrator-loop").start()
    uvicorn.run(create_app(orch), host="127.0.0.1", port=8800)


if __name__ == "__main__":  # pragma: no cover
    main()
