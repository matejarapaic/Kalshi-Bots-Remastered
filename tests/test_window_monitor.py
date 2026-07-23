"""window-monitor tests. Spec: skills/window-monitor/SKILL.md.

Grammar fixtures are real tickers captured live from Kalshi's markets
endpoint on 2026-07-22 (grammar_verified date in the spec) — not synthetic
examples, per testing conventions.
"""
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bots.skills.window_monitor import (
    NEGATIVE_TTL_S, TickerGrammarError, WindowResolver, active_window,
    event_ticker_for_close, market_ticker_for_close, next_quarter_close,
    parse_market_ticker, window_phase,
)

UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


# (market_ticker, close_time_utc) pairs captured verbatim from the live API
LIVE_CAPTURED = [
    ("KXBTC15M-26JUL222100-00", utc(2026, 7, 23, 1, 0)),
    ("KXBTC15M-26JUL222115-15", utc(2026, 7, 23, 1, 15)),
    ("KXBTC15M-26JUL222130-30", utc(2026, 7, 23, 1, 30)),
    ("KXBTC15M-26JUL232345-45", utc(2026, 7, 24, 3, 45)),
    ("KXBTC15M-26JUL240000-00", utc(2026, 7, 24, 4, 0)),   # ET-midnight rollover
]


class TestGrammar:
    @pytest.mark.parametrize("ticker,close", LIVE_CAPTURED)
    def test_construct_matches_live_capture(self, ticker, close):
        assert market_ticker_for_close(close) == ticker

    @pytest.mark.parametrize("ticker,close", LIVE_CAPTURED)
    def test_parse_roundtrip(self, ticker, close):
        series, parsed_close = parse_market_ticker(ticker)
        assert series == "KXBTC15M"
        assert parsed_close == close

    def test_event_ticker_is_market_ticker_minus_suffix(self):
        close = utc(2026, 7, 23, 1, 30)
        assert event_ticker_for_close(close) == "KXBTC15M-26JUL222130"

    def test_est_window_uses_wall_clock_not_fixed_offset(self):
        # January is EST (UTC-5): 21:30 ET closes at 02:30Z next day. A fixed
        # -4 offset (EDT hardcode) would emit 2230 here.
        close = utc(2026, 1, 16, 2, 30)
        assert market_ticker_for_close(close) == "KXBTC15M-26JAN152130-30"

    def test_dst_end_fold_hour_collides_and_parse_picks_first(self):
        # DST ends 2026-11-01: 01:30 ET happens twice (05:30Z EDT, 06:30Z EST).
        # The grammar cannot distinguish them — both construct the same ticker
        # (documented UNVERIFIED edge; API close_time verification is the
        # authority) and parse resolves to the first occurrence.
        first = utc(2026, 11, 1, 5, 30)
        second = utc(2026, 11, 1, 6, 30)
        t1 = market_ticker_for_close(first)
        t2 = market_ticker_for_close(second)
        assert t1 == t2 == "KXBTC15M-26NOV010130-30"
        _, parsed = parse_market_ticker(t1)
        assert parsed == first

    def test_bad_grammar_raises(self):
        for bad in ["KXBTC15M-26XXX222130-30", "KXBTC15M-26JUL2221-30",
                    "KXBTC15M-26JUL222130-45", "KXBTC15M-26JUL222137-37",
                    "garbage"]:
            with pytest.raises(TickerGrammarError):
                parse_market_ticker(bad)


class TestWindowMath:
    def test_boundary_instant_belongs_to_new_window(self):
        # at exactly 01:30:00Z the 0115-0130 window has settled; the active
        # window is 0130-0145
        assert next_quarter_close(utc(2026, 7, 23, 1, 30)) == utc(2026, 7, 23, 1, 45)

    def test_mid_window(self):
        assert next_quarter_close(utc(2026, 7, 23, 1, 17, 44)) == utc(2026, 7, 23, 1, 30)

    def test_active_window_fields(self):
        w = active_window(utc(2026, 7, 23, 1, 20))
        assert w.market_ticker == "KXBTC15M-26JUL222130-30"
        assert w.opens_at == utc(2026, 7, 23, 1, 15)
        assert w.closes_at == utc(2026, 7, 23, 1, 30)
        assert w.strike is None  # pure construction never knows the strike

    def test_naive_datetime_rejected(self):
        from kalshi_bots.skills.window_monitor import WindowMonitorError
        with pytest.raises(WindowMonitorError):
            next_quarter_close(datetime(2026, 7, 23, 1, 20))


class TestPhases:
    def make_window(self):
        return active_window(utc(2026, 7, 23, 1, 16))  # 0115-0130 window

    def test_full_lifecycle_timeline(self):
        w = self.make_window()
        open_ = w.opens_at
        assert window_phase(open_, w) == "opening"
        assert window_phase(open_ + timedelta(seconds=119), w) == "opening"
        assert window_phase(open_ + timedelta(seconds=120), w) == "midpoint"
        assert window_phase(w.closes_at - timedelta(seconds=181), w) == "midpoint"
        assert window_phase(w.closes_at - timedelta(seconds=180), w) == "near_close"
        assert window_phase(w.closes_at - timedelta(seconds=1), w) == "near_close"
        assert window_phase(w.closes_at, w) == "settled"
        assert window_phase(w.closes_at + timedelta(hours=1), w) == "settled"

    def test_before_open_clamps_to_opening(self):
        w = self.make_window()
        assert window_phase(w.opens_at - timedelta(minutes=5), w) == "opening"


class FakeKalshi:
    """Programmable get_market_raw for verification tests."""

    def __init__(self):
        self.markets: dict[str, dict] = {}
        self.calls = 0

    def get_market_raw(self, ticker: str) -> dict:
        self.calls += 1
        if ticker not in self.markets:
            raise LookupError(f"404 {ticker}")
        return self.markets[ticker]


class TestResolution:
    NOW = utc(2026, 7, 23, 1, 20)
    TICKER = "KXBTC15M-26JUL222130-30"

    def market(self, **over):
        base = {"ticker": self.TICKER, "status": "active",
                "close_time": "2026-07-23T01:30:00Z", "floor_strike": 66010.86}
        base.update(over)
        return base

    def test_verified_window_carries_strike(self):
        k = FakeKalshi()
        k.markets[self.TICKER] = self.market()
        w = WindowResolver(k).resolve_active(self.NOW)
        assert w is not None
        assert w.strike == 66010.86
        assert w.market_ticker == self.TICKER

    def test_missing_market_resolves_none_never_guesses(self):
        w = WindowResolver(FakeKalshi()).resolve_active(self.NOW)
        assert w is None

    def test_close_time_mismatch_resolves_none(self):
        # the DST-fold / grammar-drift guard: API says a different close
        k = FakeKalshi()
        k.markets[self.TICKER] = self.market(close_time="2026-07-23T02:30:00Z")
        assert WindowResolver(k).resolve_active(self.NOW) is None

    def test_non_tradeable_status_resolves_none(self):
        k = FakeKalshi()
        k.markets[self.TICKER] = self.market(status="initialized")
        assert WindowResolver(k).resolve_active(self.NOW) is None

    def test_verified_result_is_cached(self):
        k = FakeKalshi()
        k.markets[self.TICKER] = self.market()
        m = WindowResolver(k)
        m.resolve_active(self.NOW)
        m.resolve_active(self.NOW + timedelta(seconds=30))
        assert k.calls == 1

    def test_missing_strike_refetches_until_present(self):
        # market verified but floor_strike not yet stamped (first moments
        # after open): resolve returns a window without strike and re-asks
        k = FakeKalshi()
        k.markets[self.TICKER] = self.market(floor_strike=None)
        m = WindowResolver(k)
        w = m.resolve_active(self.NOW)
        assert w is not None and w.strike is None
        k.markets[self.TICKER] = self.market()
        w = m.resolve_active(self.NOW + timedelta(seconds=5))
        assert w.strike == 66010.86
        assert k.calls == 2

    def test_negative_cache_expires(self, monkeypatch):
        import kalshi_bots.skills.window_monitor as wm
        k = FakeKalshi()
        m = WindowResolver(k)
        clock = {"t": 1000.0}
        monkeypatch.setattr(wm.time, "monotonic", lambda: clock["t"])
        assert m.resolve_active(self.NOW) is None
        assert m.resolve_active(self.NOW) is None      # cached negative
        assert k.calls == 1
        k.markets[self.TICKER] = self.market()
        clock["t"] += NEGATIVE_TTL_S + 1               # TTL expiry -> retry
        assert m.resolve_active(self.NOW) is not None
        assert k.calls == 2

    def test_cache_is_bounded(self):
        from kalshi_bots.skills.window_monitor import VERIFY_CACHE_MAX
        k = FakeKalshi()
        m = WindowResolver(k)
        t = utc(2026, 7, 23, 0, 1)
        for i in range(VERIFY_CACHE_MAX + 10):
            m.resolve_active(t + timedelta(minutes=15 * i))
        assert len(m._cache) <= VERIFY_CACHE_MAX
