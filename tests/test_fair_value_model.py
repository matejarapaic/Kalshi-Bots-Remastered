"""fair-value-model tests. Spec: skills/fair-value-model/SKILL.md.

Reference values hand-computed: p = Phi(ln(S/K) / (sigma*sqrt(tau))),
tau = seconds/31_536_000.
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bots.skills.fair_value_model import (
    FairValueError, evaluate, fair_value_prob, side_edges,
)
from kalshi_bots.types import CompositeSpot, MarketRef, OrderbookSnapshot, WindowRef

NOW = datetime(2026, 7, 23, 1, 20, tzinfo=timezone.utc)


def spot(mid=66000.0, healthy=5):
    return CompositeSpot(mid=mid, bid=mid - 0.5, ask=mid + 0.5, source_ts={},
                         computed_at=NOW, constituents_healthy=healthy,
                         constituent_count=5)


def window(strike=66000.0, closes_in_s=450.0):
    closes = NOW + timedelta(seconds=closes_in_s)
    return WindowRef(series_ticker="KXBTC15M",
                     event_ticker="KXBTC15M-26JUL222130",
                     market_ticker="KXBTC15M-26JUL222130-30",
                     opens_at=closes - timedelta(seconds=900), closes_at=closes,
                     strike=strike)


def book(yes_ask=50, no_ask=52):
    m = MarketRef(family="crypto", series_ticker="KXBTC15M",
                  event_ticker="E", market_ticker="T", yes_label="up",
                  title="", close_ts=None, settlement_notes=None)
    return OrderbookSnapshot(market=m, yes_bid=100 - no_ask, yes_ask=yes_ask,
                             no_bid=100 - yes_ask, no_ask=no_ask,
                             yes_book=[], no_book=[], devigged_yes_prob=None,
                             spread_cents=None, fetched_at=NOW)


class TestFairValueProb:
    def test_at_the_money_is_exactly_half(self):
        # ln(S/K)=0 regardless of sigma/tau -> Phi(0) = 0.5 exactly
        assert fair_value_prob(66000.0, 66000.0, 450.0, 0.6) == 0.5

    def test_hand_computed_above_strike(self):
        # S=66100, K=66000, sigma=0.6, tau=450s:
        # d = ln(66100/66000) / (0.6*sqrt(450/31_536_000))
        s, k, sig, t = 66100.0, 66000.0, 0.6, 450.0
        d = math.log(s / k) / (sig * math.sqrt(t / 31_536_000))
        expected = 0.5 * (1 + math.erf(d / math.sqrt(2)))
        assert fair_value_prob(s, k, t, sig) == pytest.approx(expected, abs=1e-12)
        assert fair_value_prob(s, k, t, sig) > 0.5

    def test_symmetry_around_strike(self):
        # multiplicative symmetry: p(S*r, K) + p(S/r, K) == 1 for log-normal
        up = fair_value_prob(66000.0 * 1.001, 66000.0, 450.0, 0.6)
        down = fair_value_prob(66000.0 / 1.001, 66000.0, 450.0, 0.6)
        assert up + down == pytest.approx(1.0, abs=1e-12)

    def test_more_time_pulls_toward_half(self):
        near = fair_value_prob(66100.0, 66000.0, 30.0, 0.6)
        far = fair_value_prob(66100.0, 66000.0, 890.0, 0.6)
        assert near > far > 0.5

    def test_more_vol_pulls_toward_half(self):
        calm = fair_value_prob(66100.0, 66000.0, 450.0, 0.3)
        wild = fair_value_prob(66100.0, 66000.0, 450.0, 1.5)
        assert calm > wild > 0.5

    def test_expiry_collapses_to_step_with_tie_up(self):
        # tau<=0: point mass; greater_or_equal settlement -> tie resolves YES
        assert fair_value_prob(66001.0, 66000.0, 0.0, 0.6) == 1.0
        assert fair_value_prob(66000.0, 66000.0, 0.0, 0.6) == 1.0
        assert fair_value_prob(65999.0, 66000.0, -5.0, 0.6) == 0.0

    def test_zero_sigma_collapses_to_step(self):
        assert fair_value_prob(66001.0, 66000.0, 450.0, 0.0) == 1.0
        assert fair_value_prob(65999.0, 66000.0, 450.0, 0.0) == 0.0

    def test_broken_feed_raises(self):
        with pytest.raises(FairValueError):
            fair_value_prob(0.0, 66000.0, 450.0, 0.6)
        with pytest.raises(FairValueError):
            fair_value_prob(66000.0, -1.0, 450.0, 0.6)


class TestEvaluate:
    def test_bundle_fields(self):
        est = evaluate(window(), spot(), book(yes_ask=48), sigma=0.6, now=NOW)
        assert est.model_prob_up == 0.5
        assert est.model_prob_down == 0.5
        assert est.market_ask_cents == 48
        assert est.edge_cents == pytest.approx(2.0)   # 50 - 48
        assert est.sigma_used == 0.6
        assert est.spot_used == 66000.0
        assert est.strike == 66000.0
        assert est.time_remaining_s == pytest.approx(450.0)

    def test_no_book_means_no_edge_not_a_guess(self):
        est = evaluate(window(), spot(), None, sigma=0.6, now=NOW)
        assert est.market_ask_cents is None and est.edge_cents is None

    def test_missing_strike_raises(self):
        w = window()
        w.strike = None
        with pytest.raises(FairValueError):
            evaluate(w, spot(), book(), sigma=0.6, now=NOW)


class TestSideEdges:
    def test_edge_sign_symmetry_no_spread(self):
        # model 0.55, both asks 50: up edge +5, down edge -5 (equal magnitude)
        est = evaluate(window(strike=65900.0), spot(), book(yes_ask=50, no_ask=50),
                       sigma=0.6, now=NOW)
        # pick spot/strike so model_prob_up ~ 0.55: solve instead by asserting
        # the identity directly on whatever prob came out
        edges = side_edges(est, book(yes_ask=50, no_ask=50))
        assert edges["yes"] == pytest.approx(est.model_prob_up * 100 - 50)
        assert edges["no"] == pytest.approx(est.model_prob_down * 100 - 50)
        assert edges["yes"] == pytest.approx(-edges["no"])  # zero-spread mirror

    def test_spread_makes_edges_asymmetric(self):
        b = book(yes_ask=52, no_ask=52)  # 4c of vig
        est = evaluate(window(), spot(), b, sigma=0.6, now=NOW)
        edges = side_edges(est, b)
        assert edges["yes"] == pytest.approx(-2.0)
        assert edges["no"] == pytest.approx(-2.0)  # both negative: vig eats both

    def test_empty_side_is_none(self):
        b = book()
        b.no_ask = None
        est = evaluate(window(), spot(), b, sigma=0.6, now=NOW)
        edges = side_edges(est, b)
        assert edges["no"] is None and edges["yes"] is not None
