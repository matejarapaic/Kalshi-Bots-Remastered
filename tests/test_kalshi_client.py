import os
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bots.skills.kalshi_client import (
    KalshiClient, KalshiProdRefused, build_snapshot, depth_within,
    dollars_to_cents, est_fee_cents,
)
from kalshi_bots.types import MarketRef, OrderRequest

MARKET = MarketRef(league="mlb", series_ticker="KXMLBGAME",
                   event_ticker="KXMLBGAME-26JUL191920LADNYY",
                   market_ticker="KXMLBGAME-26JUL191920LADNYY-NYY",
                   yes_team_kalshi_abbr="NYY", title="t", close_ts=None,
                   settlement_notes=None)


def ob(yes=None, no=None):
    return {"orderbook_fp": {"yes_dollars": yes or [], "no_dollars": no or []}}


class TestDollarConversion:
    def test_table(self):
        assert dollars_to_cents("0.4400") == 44
        assert dollars_to_cents("0.0100") == 1
        assert dollars_to_cents("1.0000") == 100
        assert dollars_to_cents("0.9900") == 99

    def test_fractional_quantities_floor(self):
        snap = build_snapshot(MARKET, ob(yes=[["0.4400", "6903.99"]]))
        assert snap.no_book[0].quantity == 6903  # floored, never rounded up


class TestDerivedAsks:
    def test_two_sided_normal(self):
        snap = build_snapshot(MARKET, ob(
            yes=[["0.4400", "100"], ["0.4300", "50"]],
            no=[["0.5300", "200"], ["0.5200", "75"]]))
        assert snap.yes_bid == 44 and snap.no_bid == 53
        assert snap.yes_ask == 47  # 100 - 53
        assert snap.no_ask == 56   # 100 - 44
        # YES ask ladder = NO bids mirrored, ascending, quantities preserved
        assert [(l.price, l.quantity) for l in snap.yes_book] == [(47, 200), (48, 75)]
        assert snap.spread_cents == 3

    def test_one_sided_no_yes_bids(self):
        snap = build_snapshot(MARKET, ob(no=[["0.5300", "200"]]))
        assert snap.yes_bid is None and snap.yes_ask == 47
        assert snap.devigged_yes_prob is None  # untradeable per spec rule 7
        assert snap.spread_cents is None

    def test_empty_book(self):
        snap = build_snapshot(MARKET, ob())
        assert snap.devigged_yes_prob is None
        assert snap.yes_bid is None and snap.no_bid is None
        assert snap.yes_book == [] and snap.no_book == []

    def test_crossed_book_negative_spread(self):
        # yes_bid 55, no_bid 50 -> derived yes_ask 50 < yes_bid: crossed
        snap = build_snapshot(MARKET, ob(yes=[["0.5500", "10"]], no=[["0.5000", "10"]]))
        assert snap.spread_cents == -5
        assert snap.devigged_yes_prob is not None  # still computed; consumers gate

    def test_thin_book_one_contract(self):
        snap = build_snapshot(MARKET, ob(yes=[["0.4400", "1"]], no=[["0.5500", "1"]]))
        assert snap.devigged_yes_prob is not None
        assert depth_within(snap, "yes", 2) == 1


class TestDevig:
    def test_symmetric_book_is_half(self):
        snap = build_snapshot(MARKET, ob(yes=[["0.4800", "10"]], no=[["0.4800", "10"]]))
        # yes: bid 48 ask 52 mid 50; no: bid 48 ask 52 mid 50 -> 0.5
        assert snap.devigged_yes_prob == pytest.approx(0.5)

    def test_asymmetric(self):
        snap = build_snapshot(MARKET, ob(yes=[["0.6000", "10"]], no=[["0.3500", "10"]]))
        # yes mid (60+65)/2=62.5; no mid (35+40)/2=37.5 -> 62.5/100
        assert snap.devigged_yes_prob == pytest.approx(0.625)


class TestDepth:
    def test_depth_within_window(self):
        snap = build_snapshot(MARKET, ob(
            no=[["0.5300", "200"], ["0.5100", "100"], ["0.4800", "500"]]))
        # yes asks at 47(200), 49(100), 52(500); best 47, within 2c: 47,49
        assert depth_within(snap, "yes", 2) == 300
        assert depth_within(snap, "yes", 0) == 200
        assert depth_within(snap, "yes", 10) == 800

    def test_depth_empty(self):
        assert depth_within(build_snapshot(MARKET, ob()), "yes", 2) == 0


class TestFees:
    @pytest.mark.parametrize("contracts,price,expected", [
        (1, 50, 2),      # 7*1*50*50/10000 = 1.75 -> 2
        (1, 60, 2),      # 1.68 -> 2
        (40, 60, 68),    # 67.2 -> 68 (risk spec worked example)
        (100, 50, 175),  # exact 175, no ceil bump
        (1, 1, 1),       # 0.0693 -> 1
        (1, 99, 1),
        (1000, 95, 333),  # 7*1000*95*5/10000 = 332.5 -> 333
        (0, 50, 0),
    ])
    def test_fee_table(self, contracts, price, expected):
        assert est_fee_cents(contracts, price) == expected


class TestEnvGate:
    def test_prod_refused_without_flag(self, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "prod")
        monkeypatch.delenv("KALSHI_ALLOW_PROD", raising=False)
        with pytest.raises(KalshiProdRefused):
            KalshiClient()

    def test_demo_default(self, monkeypatch):
        monkeypatch.delenv("KALSHI_ENV", raising=False)
        c = KalshiClient()
        assert c.env() == "demo"
        assert "demo" in c.host


class TestSigning:
    def test_signature_verifies_and_excludes_query(self, monkeypatch, tmp_path):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        from cryptography.hazmat.primitives import serialization
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        key_file = tmp_path / "k.pem"
        key_file.write_bytes(pem)
        monkeypatch.setenv("KALSHI_ENV", "demo")
        monkeypatch.setenv("KALSHI_KEY_ID", "kid-123")
        monkeypatch.setenv("KALSHI_KEY_PATH", str(key_file))
        c = KalshiClient()
        headers = c._headers("GET", "/portfolio/balance?foo=bar")
        assert headers["KALSHI-ACCESS-KEY"] == "kid-123"
        msg = (headers["KALSHI-ACCESS-TIMESTAMP"] + "GET"
               + "/trade-api/v2/portfolio/balance").encode()  # query stripped
        import base64
        key.public_key().verify(
            base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256())  # raises if invalid


# Real order object pulled live from the demo exchange during the
# 2026-07-18 incident (order e0ca763f...): fully executed 4-contract fill.
# The old _order_result read taker_fill_count_fp/taker_fill_count, which
# don't exist on this object, so filled_contracts silently computed to 0 —
# every real fill was logged as "declined:unfilled" and never reached the
# risk ledger or vault, causing unbounded, untracked position accumulation.
REAL_EXECUTED_ORDER = {
    "action": "buy", "book_side": "bid", "client_order_id": "kb-2156fc481c0a",
    "created_time": "2026-07-18T00:42:05.833916Z", "fill_count_fp": "4.00",
    "initial_count_fp": "4.00", "last_update_time": "2026-07-18T00:42:05.833916Z",
    "maker_fees_dollars": "0.000000", "maker_fill_cost_dollars": "0.000000",
    "no_price_dollars": "0.5100", "order_id": "e0ca763f-1f18-4718-b4b1-9c9f922dc5ab",
    "outcome_side": "yes", "remaining_count_fp": "0.00",
    "self_trade_prevention_type": "taker_at_cross", "side": "yes",
    "status": "executed", "subaccount_number": 0,
    "taker_fees_dollars": "0.070000", "taker_fill_cost_dollars": "1.960000",
    "ticker": "KXMLBGAME-26JUL171905LADNYY-NYY", "type": "limit",
    "user_id": "c197aa39-3f37-4460-8a97-d6975d0c90d5", "yes_price_dollars": "0.4900",
}


# The actual raw response from a live POST /portfolio/events/orders call
# (2026-07-18 diagnostic order, 1 contract, fully filled at 49c). This is
# the shape place_order truly has to parse in real time — it has NO
# "status" field and NO "order" wrapper, and uses fill_count (not
# fill_count_fp). A fix verified only against the GET-order shape (above)
# still silently reported this as an unfilled/rejected order.
REAL_CREATE_ORDER_RESPONSE = {
    "average_fee_paid": "0.0175", "average_fill_price": "0.4900",
    "client_order_id": "kb-diag-9688997c", "fill_count": "1.00",
    "order_id": "85f92316-1e7a-4e6c-919f-37d2af08ea19",
    "remaining_count": "0.00", "ts_ms": 1784336830522,
}


class TestOrderResultFieldNames:
    def test_real_executed_order_reports_actual_fill(self):
        result = KalshiClient._order_result(REAL_EXECUTED_ORDER)
        assert result.status == "filled"
        assert result.filled_contracts == 4
        assert result.avg_fill_price == 49
        assert result.fee_cents == 7

    def test_real_create_order_response_reports_actual_fill(self):
        result = KalshiClient._order_result(REAL_CREATE_ORDER_RESPONSE)
        assert result.status == "filled"
        assert result.filled_contracts == 1
        assert result.avg_fill_price == 49
        assert result.fee_cents == 2  # 0.0175 -> ceil-rounded cents

    def test_create_order_response_zero_fill_is_not_filled(self):
        result = KalshiClient._order_result({
            "fill_count": "0.00", "remaining_count": "0.00",
            "order_id": "x", "client_order_id": "y",
        })
        assert result.status == "canceled"
        assert result.filled_contracts == 0
        assert result.avg_fill_price is None

    def test_place_order_parses_real_flat_create_response(self, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "demo")
        monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
        c = KalshiClient()
        c._key_id, c._private_key = "kid", object()
        c._headers = lambda method, path: {}
        monkeypatch.setattr(c.session, "request", lambda *a, **k:
                            _FakeResponse(200, REAL_CREATE_ORDER_RESPONSE))
        result = c.place_order(OrderRequest(
            market_ticker=MARKET.market_ticker, side="yes", action="buy",
            contracts=1, limit_price=49, client_order_id="kb-diag-9688997c"))
        assert result.filled_contracts == 1  # was silently 0 before this fix

    def test_place_order_end_to_end_reports_the_real_fill(self, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "demo")
        monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
        c = KalshiClient()
        c._key_id, c._private_key = "kid", object()
        c._headers = lambda method, path: {}
        monkeypatch.setattr(c.session, "request", lambda *a, **k:
                            _FakeResponse(200, {"order": REAL_EXECUTED_ORDER}))
        result = c.place_order(OrderRequest(
            market_ticker=MARKET.market_ticker, side="yes", action="buy",
            contracts=4, limit_price=49, client_order_id="kb-2156fc481c0a"))
        assert result.filled_contracts == 4  # was silently 0 before the fix


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class TestPlaceOrderV2:
    """Regression coverage for the v1->v2 create-order migration: a live
    order against /portfolio/orders (v1) started getting rejected with
    410 deprecated_v1_order_endpoint, which had no test coverage and
    took down the whole orchestrator loop (see 2026-07-17 incident)."""

    def _client(self, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "demo")
        monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
        monkeypatch.delenv("KALSHI_KEY_PATH", raising=False)
        c = KalshiClient()
        c._key_id, c._private_key = "kid", object()  # bypass auth_required gate
        c._headers = lambda method, path: {}
        return c

    def test_buy_yes_posts_bid_on_v2_path(self, monkeypatch):
        c = self._client(monkeypatch)
        captured = {}

        def fake_request(method, url, json=None, headers=None, timeout=None):
            captured["method"], captured["url"], captured["body"] = method, url, json
            return _FakeResponse(200, {"order": {
                "order_id": "o1", "status": "executed",
                "fill_count_fp": "9.00", "taker_fill_cost_dollars": "4.4100",
                "taker_fees_dollars": "0.09"}})

        monkeypatch.setattr(c.session, "request", fake_request)
        result = c.place_order(OrderRequest(
            market_ticker=MARKET.market_ticker, side="yes", action="buy",
            contracts=9, limit_price=49, client_order_id="kb-1"))

        assert captured["url"].endswith("/portfolio/events/orders")
        assert captured["body"]["side"] == "bid"
        assert captured["body"]["price"] == "0.4900"
        assert captured["body"]["count"] == "9.00"
        assert "yes_price" not in captured["body"] and "action" not in captured["body"]
        assert result.filled_contracts == 9

    def test_buy_no_posts_ask_at_complement_price(self, monkeypatch):
        c = self._client(monkeypatch)
        captured = {}

        def fake_request(method, url, json=None, headers=None, timeout=None):
            captured["body"] = json
            return _FakeResponse(200, {"order": {
                "order_id": "o2", "status": "executed",
                "fill_count_fp": "5.00", "taker_fill_cost_dollars": "1.5000",
                "taker_fees_dollars": "0.03"}})

        monkeypatch.setattr(c.session, "request", fake_request)
        c.place_order(OrderRequest(
            market_ticker=MARKET.market_ticker, side="no", action="buy",
            contracts=5, limit_price=70, client_order_id="kb-2"))

        # buying NO at 70c is quoted to the v2 (YES-only) book as an ask at 1-0.70
        assert captured["body"]["side"] == "ask"
        assert captured["body"]["price"] == "0.3000"

    def test_deprecated_endpoint_rejection_raises_order_rejected(self, monkeypatch):
        from kalshi_bots.skills.kalshi_client import KalshiOrderRejected
        c = self._client(monkeypatch)

        def fake_request(method, url, json=None, headers=None, timeout=None):
            return _FakeResponse(410, {"error": {"code": "deprecated_v1_order_endpoint"}})

        monkeypatch.setattr(c.session, "request", fake_request)
        with pytest.raises(KalshiOrderRejected):
            c.place_order(OrderRequest(
                market_ticker=MARKET.market_ticker, side="yes", action="buy",
                contracts=1, limit_price=50, client_order_id="kb-3"))
