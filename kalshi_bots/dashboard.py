"""FastAPI + WebSocket dashboard.

Data contract note: minimal event contract; every event carries `kind`,
`ts`, and event-specific fields (`series`, `event_id`, `signal_type`, ...).
Full crypto state shape (active window, feed health, model-vs-market) lands
in sprint-5.

Endpoints:
  GET /api/state  -> active window, feed health, exposure, open trades, events
  GET /health     -> liveness/degradation summary for always-on monitoring
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
from datetime import datetime, timezone
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "dashboard_static"


def _window_state(orchestrator) -> dict | None:
    """Model-vs-market snapshot of the active window. Defensive getattr
    throughout: the dashboard must render whatever subset exists."""
    monitor = getattr(orchestrator, "monitor", None)
    w = getattr(monitor, "current_window", None)
    if w is None:
        return None
    now = datetime.now(timezone.utc)
    feed = getattr(orchestrator, "feed", None)
    book = getattr(orchestrator, "book", None)
    spot = feed.current_composite() if feed is not None else None
    sigma = feed.realized_vol() if feed is not None else None
    snap = book.snapshot(w.market_ticker) if book is not None else None
    model_prob = edge = None
    if spot is not None and sigma and w.strike:
        try:
            from kalshi_bots.skills.fair_value_model import evaluate
            est = evaluate(w, spot, snap, sigma, now=now)
            model_prob, edge = est.model_prob_up, est.edge_cents
        except Exception:
            pass
    return {
        "market_ticker": w.market_ticker,
        "phase": getattr(monitor, "current_phase", None),
        "closes_at": w.closes_at.isoformat(),
        "time_remaining_s": max(0, (w.closes_at - now).total_seconds()),
        "strike": w.strike,
        "spot": spot.mid if spot is not None else None,
        "sigma": sigma,
        "model_prob_up": model_prob,
        "yes_bid": snap.yes_bid if snap is not None else None,
        "yes_ask": snap.yes_ask if snap is not None else None,
        "edge_cents": edge,
    }


def _feed_state(orchestrator) -> dict:
    feed = getattr(orchestrator, "feed", None)
    book = getattr(orchestrator, "book", None)
    out: dict = {"composite_available": False, "constituents": [],
                 "kalshi_ws": None}
    if feed is not None:
        h = feed.health()
        out["composite_available"] = h.composite_available
        out["healthy_count"] = h.healthy_count
        out["constituent_count"] = h.constituent_count
        out["constituents"] = [dataclasses.asdict(c) for c in h.constituents]
    if book is not None:
        w = getattr(getattr(orchestrator, "monitor", None), "current_window", None)
        ticker = w.market_ticker if w is not None else ""
        out["kalshi_ws"] = dataclasses.asdict(book.health(ticker))
    return out


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
        trades = []
        unrealized_cents = 0
        for t in orchestrator.trader.open_trades.values():
            current_price = None
            try:
                snap = orchestrator.trader.broker.get_orderbook(t["market"])
                current_price = snap.yes_bid if t["side"] == "yes" else snap.no_bid
            except Exception:
                pass  # mark-to-market unavailable this tick; entry-only row still shown
            if current_price is not None:
                unrealized_cents += t["contracts"] * (current_price - t["entry_price"])
            trades.append({
                "family": t.get("family"), "event_id": t.get("event_id"),
                "signal_type": None,
                "skill": t["skill"], "market_ticker": t["market_ticker"],
                "side": t["side"], "contracts": t["contracts"],
                "entry_price_cents": t["entry_price"],
                "current_price_cents": current_price,
            })
        bankroll = exp.bankroll_cents
        analyst = getattr(orchestrator, "analyst", None)
        recent = list(getattr(analyst, "recent_reports", []) or [])[-4:]
        return {
            "env": "demo",
            "mode": "paper" if getattr(orchestrator, "paper", True) else "demo-exchange",
            "window": _window_state(orchestrator),
            "feed": _feed_state(orchestrator),
            "exposure": dataclasses.asdict(exp),
            "unrealized_pnl_cents": unrealized_cents,
            "unrealized_pnl_pct": (100 * unrealized_cents / bankroll) if bankroll else 0.0,
            "open_trades": trades,
            "postmortems": [dataclasses.asdict(r) for r in recent],
            "events": orchestrator.events[-100:],
        }

    @app.get("/health")
    def health():
        """Always-on liveness: 'ok' only when every streaming dependency the
        trader gates on is currently available. Degraded is not down — the
        system fails closed and keeps watching."""
        checks = {}
        feed = getattr(orchestrator, "feed", None)
        checks["composite"] = bool(feed is not None
                                   and feed.health().composite_available)
        book = getattr(orchestrator, "book", None)
        if book is None:
            checks["kalshi_ws"] = None  # not configured (paper w/o creds)
        else:
            w = getattr(getattr(orchestrator, "monitor", None),
                        "current_window", None)
            checks["kalshi_ws"] = bool(
                book.health(w.market_ticker if w else "").connected)
        checks["window_resolved"] = getattr(
            getattr(orchestrator, "monitor", None), "current_window", None) is not None
        halted, halt_reason = orchestrator.risk.halted()
        checks["not_halted"] = not halted
        ok = all(v for v in checks.values() if v is not None)
        return {"status": "ok" if ok else "degraded", "checks": checks,
                "halt_reason": halt_reason,
                "ts": datetime.now(timezone.utc).isoformat()}

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
