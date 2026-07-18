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
    assert set(s.keys()) == {"env", "exposure", "unrealized_pnl_cents",
                             "unrealized_pnl_pct", "open_trades", "events"}
    evt = s["events"][-1]
    assert {"sport", "league", "game_id", "signal_type"} <= set(evt.keys())


class FakeBroker:
    def __init__(self, yes_bid=55):
        self.yes_bid = yes_bid

    def get_orderbook(self, market):
        from kalshi_bots.types import OrderbookSnapshot
        return OrderbookSnapshot(
            market=market, yes_bid=self.yes_bid, yes_ask=self.yes_bid + 2,
            no_bid=100 - self.yes_bid - 2, no_ask=100 - self.yes_bid,
            yes_book=[], no_book=[], devigged_yes_prob=None,
            spread_cents=2, fetched_at=None)


def test_open_trades_mark_to_market_pnl():
    from kalshi_bots.types import MarketRef

    orch = FakeOrchestrator()
    orch.trader.broker = FakeBroker(yes_bid=55)  # entered at 49c, now 55c bid
    market = MarketRef(league="mlb", series_ticker="KXMLBGAME",
                       event_ticker="KXMLBGAME-26JUL171905LADNYY",
                       market_ticker="KXMLBGAME-26JUL171905LADNYY-NYY",
                       yes_team_kalshi_abbr="NYY", title="t", close_ts=None,
                       settlement_notes=None)
    orch.trader.open_trades = {"kb-1": {
        "market_ticker": market.market_ticker, "skill": "sportsbook-kalshi-divergence",
        "side": "yes", "contracts": 10, "entry_price": 49,
        "espn_event_id": "e1", "league": "mlb", "market": market,
    }}
    c = TestClient(create_app(orch))
    s = c.get("/api/state").json()
    assert s["unrealized_pnl_cents"] == 60  # 10 * (55 - 49)
    assert s["open_trades"][0]["current_price_cents"] == 55


def test_websocket_handshake_and_push(client):
    """Regression: postponed annotations made FastAPI treat the websocket
    param as a query field -> 1008 close on every handshake."""
    c, orch = client
    with c.websocket_connect("/ws") as ws:
        orch.events.append({"kind": "cycle", "ts": "2026-07-17T20:01:00+00:00",
                            "n": 0, "signals": 1})
        msg = ws.receive_json()
        assert msg["kind"] == "cycle" and msg["signals"] == 1
