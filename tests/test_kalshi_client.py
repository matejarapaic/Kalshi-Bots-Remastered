import os
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bots.skills.kalshi_client import (
    KalshiClient, KalshiProdRefused, build_snapshot, depth_within,
    dollars_to_cents, est_fee_cents,
)
from kalshi_bots.types import MarketRef

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
