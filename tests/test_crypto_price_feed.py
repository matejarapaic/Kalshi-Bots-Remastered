"""crypto-price-feed tests. Spec: skills/crypto-price-feed/SKILL.md.

All offline: ticks are injected via CryptoPriceFeed._on_tick / _sample_once
with explicit clocks — no WebSocket connections, per testing conventions.
"""
import math
from datetime import datetime, timezone

from kalshi_bots.skills.crypto_price_feed import (
    MIN_HEALTHY_CONSTITUENTS, SECONDS_PER_YEAR, STALE_CONSTITUENT_S,
    ConstituentSpec, CryptoPriceFeed, weighted_median,
)

UTC = timezone.utc


def make_feed(names, weights=None):
    specs = [
        ConstituentSpec(name=n, weight=(weights or {}).get(n, 1.0),
                        ws_url="wss://test.invalid", subscribe=None, parse=None)
        for n in names
    ]
    return CryptoPriceFeed(specs)


def tick_all(feed, quotes, mono=0.0):
    """quotes: {name: mid} — tick each constituent with a 1-dollar spread."""
    for name, mid in quotes.items():
        feed._on_tick(name, bid=mid - 0.5, ask=mid + 0.5,
                      source_ts=datetime.now(UTC), mono=mono)


class TestWeightedMedian:
    def test_three_equal_weights_picks_middle(self):
        assert weighted_median([(100.0, 1.0), (101.0, 1.0), (99.0, 1.0)]) == 100.0

    def test_single_value(self):
        assert weighted_median([(66000.0, 0.2)]) == 66000.0

    def test_four_equal_weights_averages_straddle(self):
        # cumulative weight hits exactly half between the 2nd and 3rd values
        assert weighted_median(
            [(1.0, 1.0), (2.0, 1.0), (3.0, 1.0), (4.0, 1.0)]) == 2.5

    def test_five_equal_weights_picks_middle(self):
        vals = [(10.0, 1.0), (30.0, 1.0), (20.0, 1.0), (50.0, 1.0), (40.0, 1.0)]
        assert weighted_median(vals) == 30.0

    def test_dominant_weight_wins(self):
        # one heavy constituent past the half-weight point controls the median
        assert weighted_median([(100.0, 5.0), (200.0, 1.0), (300.0, 1.0)]) == 100.0

    def test_outlier_does_not_drag_median(self):
        # a spiking exchange moves the mean but not the median
        vals = [(66000.0, 1.0), (66010.0, 1.0), (99999.0, 1.0)]
        assert weighted_median(vals) == 66010.0


class TestComposite:
    def test_median_of_three(self):
        feed = make_feed(["a", "b", "c"])
        tick_all(feed, {"a": 66000.0, "b": 66010.0, "c": 66020.0}, mono=10.0)
        spot = feed.current_composite(mono=10.5)
        assert spot is not None
        assert spot.mid == 66010.0
        assert spot.bid == 66009.5 and spot.ask == 66010.5
        assert spot.constituents_healthy == 3 and spot.constituent_count == 3

    def test_stale_constituent_excluded_from_median(self):
        feed = make_feed(["a", "b", "c", "d"])
        tick_all(feed, {"a": 66000.0, "b": 66010.0, "c": 66020.0}, mono=10.0)
        # d ticked long ago with a spike price — must not shift the composite
        feed._on_tick("d", bid=90000.0, ask=90001.0,
                      source_ts=datetime.now(UTC), mono=10.0 - STALE_CONSTITUENT_S - 1)
        spot = feed.current_composite(mono=10.5)
        assert spot.mid == 66010.0
        assert spot.constituents_healthy == 3 and spot.constituent_count == 4

    def test_fail_closed_below_min_healthy(self):
        feed = make_feed(["a", "b", "c"])
        tick_all(feed, {"a": 66000.0}, mono=10.0)
        assert MIN_HEALTHY_CONSTITUENTS == 2  # spec constant
        assert feed.current_composite(mono=10.5) is None

    def test_disconnected_constituent_is_unhealthy_even_if_recent(self):
        feed = make_feed(["a", "b"])
        tick_all(feed, {"a": 66000.0, "b": 66010.0}, mono=10.0)
        feed._on_disconnect("b")
        assert feed.current_composite(mono=10.5) is None  # 1 healthy < 2

    def test_deviant_mid_excluded_per_brti_erroneous_data_rule(self):
        # c's mid is ~6% off the median of venue mids -> excluded (BRTI's
        # potentially-erroneous-data parameter is 5%)
        feed = make_feed(["a", "b", "c"])
        tick_all(feed, {"a": 66000.0, "b": 66010.0, "c": 70000.0}, mono=10.0)
        spot = feed.current_composite(mono=10.5)
        assert spot.mid == 66005.0  # median of the two survivors
        assert spot.constituents_healthy == 2
        assert "c" not in spot.source_ts

    def test_two_mutually_deviant_constituents_fail_closed(self):
        # two venues 15% apart: can't tell which is wrong -> no composite
        feed = make_feed(["a", "b"])
        tick_all(feed, {"a": 60000.0, "b": 70000.0}, mono=10.0)
        assert feed.current_composite(mono=10.5) is None


class TestHealth:
    def test_health_reports_per_constituent(self):
        feed = make_feed(["a", "b", "c"])
        tick_all(feed, {"a": 66000.0, "b": 66010.0}, mono=10.0)
        h = feed.health(mono=11.0)
        assert h.constituent_count == 3 and h.healthy_count == 2
        by_name = {c.name: c for c in h.constituents}
        assert by_name["a"].healthy and by_name["b"].healthy
        assert not by_name["c"].healthy
        assert by_name["c"].last_tick_age_s is None  # never ticked
        assert by_name["a"].last_tick_age_s == 1.0
        assert h.composite_available

    def test_health_degrades_as_ticks_age(self):
        feed = make_feed(["a", "b"])
        tick_all(feed, {"a": 66000.0, "b": 66010.0}, mono=10.0)
        h = feed.health(mono=10.0 + STALE_CONSTITUENT_S + 0.1)
        assert h.healthy_count == 0
        assert not h.composite_available


class TestRealizedVol:
    def make_sampled_feed(self, log_returns, dt=1.0, p0=66000.0):
        """Build a feed whose vol buffer holds a price path with the given
        1-step log returns, sampled every dt seconds."""
        feed = make_feed(["a", "b"])
        t, p = 0.0, p0
        tick_all(feed, {"a": p, "b": p}, mono=t)
        feed._sample_once(mono=t, wall=datetime.now(UTC))
        for r in log_returns:
            t += dt
            p *= math.exp(r)
            tick_all(feed, {"a": p, "b": p}, mono=t)
            feed._sample_once(mono=t, wall=datetime.now(UTC))
        return feed, t

    def test_alternating_returns_match_hand_computed_vol(self):
        # +r/-r alternating: population std of returns is exactly r, so the
        # annualized vol must be r * sqrt(seconds-per-year).
        r = 1e-4
        returns = [r if i % 2 == 0 else -r for i in range(900)]
        feed, t_end = self.make_sampled_feed(returns)
        vol = feed.realized_vol(window_s=900, mono=t_end)
        expected = r * math.sqrt(SECONDS_PER_YEAR)  # ~0.5616 annualized
        assert vol is not None
        assert abs(vol - expected) / expected < 1e-6

    def test_constant_price_zero_vol(self):
        feed, t_end = self.make_sampled_feed([0.0] * 900)
        assert feed.realized_vol(window_s=900, mono=t_end) == 0.0

    def test_gap_normalization(self):
        # identical per-step returns at dt=2 must produce the same annualized
        # vol as dt=1 after sqrt(dt) normalization... scaled by 1/sqrt(2):
        # a ±r move over 2s is a ±r/sqrt(2)-per-sqrt-second move.
        r = 1e-4
        returns = [r if i % 2 == 0 else -r for i in range(450)]
        feed, t_end = self.make_sampled_feed(returns, dt=2.0)
        vol = feed.realized_vol(window_s=900, mono=t_end)
        expected = (r / math.sqrt(2.0)) * math.sqrt(SECONDS_PER_YEAR)
        assert abs(vol - expected) / expected < 1e-6

    def test_insufficient_coverage_returns_none(self):
        # only 60s of samples cannot answer for a 900s window
        feed, t_end = self.make_sampled_feed([1e-4] * 60)
        assert feed.realized_vol(window_s=900, mono=t_end) is None

    def test_small_window_is_answerable(self):
        # the sample floor scales down with the window: a 60s window must be
        # answerable from ~40s of 1s samples (a fixed 60-sample floor would
        # make window_s=60 permanently None — live smoke-run finding)
        r = 1e-4
        returns = [r if i % 2 == 0 else -r for i in range(40)]
        feed, t_end = self.make_sampled_feed(returns)
        assert feed.realized_vol(window_s=60, mono=t_end) is not None

    def test_unhealthy_composite_leaves_gap_not_garbage(self):
        # when the composite is unavailable the sampler appends nothing
        feed = make_feed(["a", "b"])
        tick_all(feed, {"a": 66000.0}, mono=0.0)  # 1 healthy < 2 -> None
        feed._sample_once(mono=0.0, wall=datetime.now(UTC))
        assert len(feed._samples) == 0


class TestMemoryBounds:
    def test_sample_buffer_is_bounded(self):
        feed = make_feed(["a", "b"])
        for i in range(feed._samples.maxlen + 500):
            tick_all(feed, {"a": 66000.0, "b": 66000.0}, mono=float(i))
            feed._sample_once(mono=float(i), wall=datetime.now(UTC))
        assert len(feed._samples) == feed._samples.maxlen
