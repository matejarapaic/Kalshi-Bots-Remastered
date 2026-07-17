import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from kalshi_bots.dashboard import create_app  # noqa: E402


class FakeRisk:
    def exposure(self):
        from kalshi_bots.types import ExposureSummary
        return ExposureSummary(bankroll_cents=10000, open_cost_cents=0,
                               by_game={}, by_skill={}, open_positions=0,
                               daily_realized_pnl_cents=0, halted=False,
                               halt_reason=None)


class FakeOrchestrator:
    def __init__(self):
        self.risk = FakeRisk()
        self.events = []

        class T:
            open_trades = {}
        self.trader = T()


@pytest.fixture
def client():
    orch = FakeOrchestrator()
    orch.events.append({"kind": "signal", "ts": "2026-07-17T20:00:00+00:00",
                        "sport": "mlb", "league": "mlb", "game_id": "e1",
                        "signal_type": "garbage-time-candidate",
                        "market_ticker": "T"})
    return TestClient(create_app(orch)), orch


def test_index_serves_html(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "KALSHI" in r.text


def test_state_contract_shape(client):
    c, _ = client
    s = c.get("/api/state").json()
    assert set(s.keys()) == {"env", "exposure", "open_trades", "events"}
    evt = s["events"][-1]
    assert {"sport", "league", "game_id", "signal_type"} <= set(evt.keys())


def test_websocket_handshake_and_push(client):
    """Regression: postponed annotations made FastAPI treat the websocket
    param as a query field -> 1008 close on every handshake."""
    c, orch = client
    with c.websocket_connect("/ws") as ws:
        orch.events.append({"kind": "cycle", "ts": "2026-07-17T20:01:00+00:00",
                            "n": 0, "signals": 1})
        msg = ws.receive_json()
        assert msg["kind"] == "cycle" and msg["signals"] == 1
