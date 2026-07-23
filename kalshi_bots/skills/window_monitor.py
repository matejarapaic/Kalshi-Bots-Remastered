"""window-monitor skill. Spec: skills/window-monitor/SKILL.md.

Resolves which 15-minute crypto contract is live for a given wall-clock time
and tracks window lifecycle phases. This is the system's entity-resolution
skill ("what should I be trading right now?"), with a hard invariant carried
over from its predecessor: **a window that cannot be verified against the
live API is None, never a guess.**

Ticker grammar (grammar_verified: 2026-07-22, live prod + demo markets):
    event ticker:  {SERIES}-{YY}{MON}{DD}{HHMM}
    market ticker: {event}-{MM}
where YY/MON/DD/HHMM encode the window CLOSE time in US Eastern *wall clock*
(EDT or EST as in effect that day — derived via America/New_York, never a
fixed UTC offset), MON is an uppercase 3-letter month, and MM duplicates the
close minute (00/15/30/45). Example: KXBTC15M-26JUL222130-30 closes
2026-07-22 21:30 ET = 2026-07-23 01:30Z.

DST caveat (UNVERIFIED, documented in SKILL.md): during the repeated
1:00-2:00 AM hour when DST ends, ET wall-clock labels are ambiguous and this
grammar could collide. resolve_active() therefore always verifies the
constructed ticker's close_time against the API and returns None on mismatch.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kalshi_bots.types import Phase, WindowRef

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

DEFAULT_SERIES = "KXBTC15M"
WINDOW_S = 900                 # quarter-hour contract length
OPENING_PHASE_S = 120          # [open, open+120s): strike/book still settling
NEAR_CLOSE_PHASE_S = 180       # [close-180s, close): gamma zone, no entries
TRADEABLE_MARKET_STATUSES = {"active", "open"}
VERIFY_CACHE_MAX = 16          # bounded: ~4 windows/hour, pruned on insert
NEGATIVE_TTL_S = 10.0          # retry failed verifications (initialized->active
                               # transition happens right at window open)

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


class WindowMonitorError(Exception):
    pass


class TickerGrammarError(WindowMonitorError):
    pass


# --- pure time math / grammar ---

def next_quarter_close(now: datetime) -> datetime:
    """The close time (UTC) of the window containing `now`: the next
    quarter-hour boundary strictly after now (a boundary instant belongs to
    the window that just opened, not the one that just settled)."""
    if now.tzinfo is None:
        raise WindowMonitorError("naive datetime — timestamps are tz-aware UTC")
    now = now.astimezone(UTC)
    base = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    return base + timedelta(seconds=WINDOW_S)


def event_ticker_for_close(close_utc: datetime, series: str = DEFAULT_SERIES) -> str:
    et = close_utc.astimezone(ET)
    return (f"{series}-{et.year % 100:02d}{_MONTHS[et.month - 1]}{et.day:02d}"
            f"{et.hour:02d}{et.minute:02d}")


def market_ticker_for_close(close_utc: datetime, series: str = DEFAULT_SERIES) -> str:
    et = close_utc.astimezone(ET)
    return f"{event_ticker_for_close(close_utc, series)}-{et.minute:02d}"


def parse_market_ticker(ticker: str) -> tuple[str, datetime]:
    """Inverse grammar: market ticker -> (series, close_utc). During the
    repeated DST-end hour the ET wall clock is ambiguous; this resolves to the
    first occurrence (fold=0) — resolve_active()'s API verification is the
    authority whenever it matters."""
    try:
        series, stamp, mm = ticker.rsplit("-", 2)
        yy, mon, dd, hhmm = stamp[:2], stamp[2:5], stamp[5:7], stamp[7:11]
        if len(stamp) != 11 or mon not in _MONTHS or mm != hhmm[2:]:
            raise ValueError(f"bad stamp {stamp!r}")
        et_local = datetime(2000 + int(yy), _MONTHS.index(mon) + 1, int(dd),
                            int(hhmm[:2]), int(hhmm[2:]), tzinfo=ET)
    except (ValueError, IndexError) as e:
        raise TickerGrammarError(f"unparseable market ticker {ticker!r}: {e}") from e
    if et_local.minute % 15 != 0:
        raise TickerGrammarError(f"{ticker!r}: close not on a quarter hour")
    return series, et_local.astimezone(UTC)


def active_window(now: datetime, series: str = DEFAULT_SERIES) -> WindowRef:
    """Pure construction of the window containing `now` (strike unknown).
    15-minute windows tile the clock 24/7, so there is always one — but it is
    NOT tradeable until resolve_active() has verified it against the API."""
    closes_at = next_quarter_close(now)
    return WindowRef(
        series_ticker=series,
        event_ticker=event_ticker_for_close(closes_at, series),
        market_ticker=market_ticker_for_close(closes_at, series),
        opens_at=closes_at - timedelta(seconds=WINDOW_S),
        closes_at=closes_at,
        strike=None,
    )


def window_phase(now: datetime, w: WindowRef) -> Phase:
    """Lifecycle phase of `w` at `now`. Times before open clamp to "opening"
    (demo markets open early; the settlement timeline is what matters)."""
    if now.tzinfo is None:
        raise WindowMonitorError("naive datetime — timestamps are tz-aware UTC")
    if now >= w.closes_at:
        return "settled"
    if (w.closes_at - now).total_seconds() <= NEAR_CLOSE_PHASE_S:
        return "near_close"
    if (now - w.opens_at).total_seconds() < OPENING_PHASE_S:
        return "opening"
    return "midpoint"


# --- API-verified resolution ---

class WindowResolver:
    """Stateful resolver: constructs the expected ticker from the clock, then
    verifies it against the live markets endpoint before anyone trades it.
    Verification results are cached per market ticker (bounded)."""

    def __init__(self, kalshi_client, series: str = DEFAULT_SERIES):
        self.kalshi = kalshi_client
        self.series = series
        # ticker -> (verified_ok, strike, fetched_mono); pruned by insertion
        self._cache: dict[str, tuple[bool, float | None, float]] = {}

    def resolve_active(self, now: datetime) -> WindowRef | None:
        """The verified, tradeable window containing `now`, or None:
        unverifiable ticker, close_time mismatch (e.g. DST fold), or a market
        status that isn't trading. `strike` may still be None for the first
        moments after open if Kalshi hasn't stamped floor_strike yet — callers
        that need the strike must gate on it."""
        w = active_window(now, self.series)
        cached = self._cache.get(w.market_ticker)
        if cached is not None:
            ok, strike, fetched_mono = cached
            if not ok:
                # negative results expire quickly: a market can flip
                # initialized->active moments after the window opens
                if time.monotonic() - fetched_mono <= NEGATIVE_TTL_S:
                    return None
            elif strike is not None:
                w.strike = strike
                return w
            # else: verified but strike still unknown — re-fetch below
        try:
            raw = self.kalshi.get_market_raw(w.market_ticker)
        except Exception as e:
            log.warning("window %s not resolvable: %s", w.market_ticker, e)
            self._remember(w.market_ticker, False, None)
            return None
        ok, strike = self._verify(w, raw)
        self._remember(w.market_ticker, ok, strike)
        if not ok:
            return None
        w.strike = strike
        return w

    def strike_for_window(self, w: WindowRef) -> float | None:
        """The verified strike for `w` (None until Kalshi stamps it at open).
        Live grammar 2026-07-22: one binary market per window; the strike is
        the prior window's 60s-BRTI settlement average, not a strike ladder."""
        cached = self._cache.get(w.market_ticker)
        if cached is not None and cached[1] is not None:
            return cached[1]
        try:
            raw = self.kalshi.get_market_raw(w.market_ticker)
        except Exception:
            return None
        ok, strike = self._verify(w, raw)
        self._remember(w.market_ticker, ok, strike)
        return strike if ok else None

    def _verify(self, w: WindowRef, raw: dict) -> tuple[bool, float | None]:
        close_s = raw.get("close_time")
        if not close_s:
            log.warning("market %s has no close_time — refusing to trust it",
                        w.market_ticker)
            return False, None
        try:
            close_api = datetime.fromisoformat(close_s.replace("Z", "+00:00"))
        except ValueError:
            return False, None
        if close_api != w.closes_at:
            # grammar drift or DST wall-clock collision: never guess
            log.error("close_time mismatch for %s: expected %s, API says %s",
                      w.market_ticker, w.closes_at.isoformat(), close_api.isoformat())
            return False, None
        if raw.get("status") not in TRADEABLE_MARKET_STATUSES:
            log.info("window %s status=%s — not tradeable",
                     w.market_ticker, raw.get("status"))
            return False, None
        strike = raw.get("floor_strike")
        return True, (float(strike) if strike is not None else None)

    def _remember(self, ticker: str, ok: bool, strike: float | None) -> None:
        self._cache[ticker] = (ok, strike, time.monotonic())
        while len(self._cache) > VERIFY_CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
