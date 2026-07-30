import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from kalshi_bots.dashboard import create_app  # noqa: E402


class FakeRisk:
    def exposure(self):
        from kalshi_bots.types import ExposureSummary
        return ExposureSummary(bankroll_cents=10000, open_cost_cents=0,
                               by_event={}, by_skill={}, open_positions=0,
                               daily_realized_pnl_cents=0, halted=False,
                               halt_reason=None)

    def halted(self):
        return False, None


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
                        "series": "KXBTC15M", "event_id": "e1",
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
    assert set(s.keys()) == {"env", "mode", "window", "feed", "exposure",
                             "unrealized_pnl_cents", "unrealized_pnl_pct",
                             "open_trades", "postmortems", "recent_trades", "events"}
    evt = s["events"][-1]
    assert {"series", "event_id", "signal_type"} <= set(evt.keys())
    assert s["window"] is None  # no monitor on the fake -> renders empty
    assert s["feed"]["composite_available"] is False


def test_window_state_renders_model_vs_market():
    from datetime import datetime, timedelta, timezone
    from kalshi_bots.types import (
        CompositeSpot, DepthLevel, MarketRef, OrderbookSnapshot, WindowRef,
    )

    now = datetime.now(timezone.utc)
    w = WindowRef(series_ticker="KXBTC15M", event_ticker="E",
                  market_ticker="E-30", opens_at=now - timedelta(minutes=5),
                  closes_at=now + timedelta(minutes=10), strike=65900.0)

    class Monitor:
        current_window = w
        current_phase = "midpoint"

    class Feed:
        def current_composite(self):
            return CompositeSpot(mid=66000.0, bid=65999.5, ask=66000.5,
                                 source_ts={}, computed_at=now,
                                 constituents_healthy=5, constituent_count=5)

        def realized_vol(self, window_s=900):
            return 0.6

        def health(self):
            from kalshi_bots.types import FeedHealth
            return FeedHealth(constituents=[], healthy_count=5,
                              constituent_count=5, composite_available=True)

    class Book:
        def snapshot(self, ticker):
            m = MarketRef(family="crypto", series_ticker="KXBTC15M",
                          event_ticker="E", market_ticker="E-30",
                          yes_label="up", title="", close_ts=None,
                          settlement_notes=None)
            return OrderbookSnapshot(market=m, yes_bid=53, yes_ask=55,
                                     no_bid=45, no_ask=47,
                                     yes_book=[DepthLevel(55, 500)],
                                     no_book=[DepthLevel(47, 500)],
                                     devigged_yes_prob=0.54, spread_cents=2,
                                     fetched_at=now)

        def health(self, ticker):
            from kalshi_bots.types import BookHealth
            return BookHealth(market_ticker=ticker, connected=True,
                              subscribed=True, last_update_age_s=0.5,
                              seq_gap=False, healthy=True)

    orch = FakeOrchestrator()
    orch.monitor, orch.feed, orch.book = Monitor(), Feed(), Book()
    s = TestClient(create_app(orch)).get("/api/state").json()
    win = s["window"]
    assert win["market_ticker"] == "E-30" and win["phase"] == "midpoint"
    assert win["strike"] == 65900.0 and win["spot"] == 66000.0
    assert win["model_prob_up"] > 0.5          # spot above strike
    assert win["edge_cents"] == pytest.approx(win["model_prob_up"] * 100 - 55)
    assert win["no_bid"] == 45 and win["no_ask"] == 47
    model_prob_down = 1.0 - win["model_prob_up"]
    assert win["edge_no_cents"] == pytest.approx(model_prob_down * 100 - 47)
    assert s["feed"]["kalshi_ws"]["healthy"] is True


def test_health_endpoint_reports_degraded_and_ok():
    orch = FakeOrchestrator()
    c = TestClient(create_app(orch))
    h = c.get("/health").json()
    # bare fake: no feed composite, no window -> degraded but alive
    assert h["status"] == "degraded"
    assert h["checks"]["composite"] is False
    assert h["checks"]["kalshi_ws"] is None     # not configured
    assert h["checks"]["not_halted"] is True


def test_health_kalshi_ws_uses_per_ticker_health_not_connected():
    """Regression: /health tested only .connected, so a WS that was up but
    had no snapshot for the active ticker (or a seq gap, or a stale book)
    reported ok while the widget — and the trader's gates — saw unhealthy.
    The rollover state (connected, subscribed, no first update yet) must
    read degraded."""
    from kalshi_bots.types import BookHealth

    class RolloverBook:
        def health(self, ticker):
            return BookHealth(market_ticker=ticker, connected=True,
                              subscribed=True, last_update_age_s=None,
                              seq_gap=False, healthy=False)

    orch = FakeOrchestrator()
    orch.book = RolloverBook()
    h = TestClient(create_app(orch)).get("/health").json()
    assert h["checks"]["kalshi_ws"] is False
    assert h["status"] == "degraded"


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
    market = MarketRef(family="crypto", series_ticker="KXBTC15M",
                       event_ticker="KXBTC15M-26JUL222130",
                       market_ticker="KXBTC15M-26JUL222130-30",
                       yes_label="up", title="t", close_ts=None,
                       settlement_notes=None)
    orch.trader.open_trades = {"kb-1": {
        "market_ticker": market.market_ticker, "skill": "btc-15min-fair-value",
        "side": "yes", "contracts": 10, "entry_price": 49,
        "event_id": "e1", "family": "crypto", "market": market,
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
