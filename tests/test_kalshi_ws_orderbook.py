"""kalshi-ws-orderbook tests. Spec: skills/kalshi-ws-orderbook/SKILL.md.

All offline: WS messages are injected via _handle_message with explicit
clocks. Message shapes mirror docs.kalshi.com asyncapi.yaml examples and the
live KXBTC15M book captured 2026-07-22.
"""
import asyncio
from datetime import datetime, timezone

from kalshi_bots.skills.kalshi_ws_orderbook import STALE_BOOK_S, KalshiOrderBook
from kalshi_bots.types import MarketRef

TICKER = "KXBTC15M-26JUL222130-30"


class StubClient:
    def env(self):
        return "demo"


def make_market():
    return MarketRef(family="crypto", series_ticker="KXBTC15M",
                     event_ticker="KXBTC15M-26JUL222130",
                     market_ticker=TICKER, yes_label="up", title="",
                     close_ts=datetime(2026, 7, 23, 1, 30, tzinfo=timezone.utc),
                     settlement_notes=None)


def make_book(subscribed=True, connected=True):
    book = KalshiOrderBook(StubClient(), ws_url="wss://test.invalid",
                           want_brti=True)
    if subscribed:
        asyncio.run(book.subscribe(make_market()))
    book._connected = connected
    return book


def snapshot_msg(seq=2, sid=7):
    # values from the live prod book capture 2026-07-22 (trimmed)
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": {
        "market_ticker": TICKER,
        "yes_dollars_fp": [["0.2800", "961.00"], ["0.2900", "451.00"],
                           ["0.3000", "6951.00"]],
        "no_dollars_fp": [["0.6300", "636.02"], ["0.6400", "846.09"]],
    }}


def delta_msg(seq, price="0.2900", delta="-451.00", side="yes", sid=7):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq, "msg": {
        "market_ticker": TICKER, "price_dollars": price, "delta_fp": delta,
        "side": side, "ts_ms": 1784769841000}}


class TestSnapshot:
    def test_snapshot_builds_derived_asks_and_devig(self):
        book = make_book()
        book._handle_message(snapshot_msg(), mono=10.0)
        snap = book.snapshot(TICKER)
        assert snap is not None
        assert snap.yes_bid == 30 and snap.no_bid == 64   # best bid = highest
        assert snap.yes_ask == 36 and snap.no_ask == 70   # 100 - other bid
        assert snap.spread_cents == 6
        assert 0.0 < snap.devigged_yes_prob < 0.5
        # fractional fp counts floor to ints in the ladder (never round up)
        no_asks_for_yes_buyer = {l.price: l.quantity for l in snap.yes_book}
        assert no_asks_for_yes_buyer[37] == 636  # from 636.02 no-bid at 63c

    def test_no_data_yet_returns_none(self):
        book = make_book()
        assert book.snapshot(TICKER) is None

    def test_unknown_ticker_message_ignored(self):
        book = make_book(subscribed=False)
        book._handle_message(snapshot_msg(), mono=10.0)
        assert book.snapshot(TICKER) is None

    def test_subcent_levels_aggregate_to_cent_buckets(self):
        book = make_book()
        msg = snapshot_msg()
        msg["msg"]["yes_dollars_fp"] = [["0.9410", "10.00"], ["0.9430", "5.50"]]
        msg["msg"]["no_dollars_fp"] = [["0.0400", "100.00"]]
        book._handle_message(msg, mono=10.0)
        snap = book.snapshot(TICKER)
        assert snap.yes_bid == 94
        assert {l.price: l.quantity for l in snap.no_book}[6] == 15  # 10+5.5 floored


class TestDeltas:
    def test_delta_applies_and_removes_levels(self):
        book = make_book()
        book._handle_message(snapshot_msg(seq=2), mono=10.0)
        book._handle_message(delta_msg(seq=3, price="0.2900", delta="-451.00"),
                             mono=11.0)
        snap = book.snapshot(TICKER)
        yes_bids = {l.price: l.quantity for l in snap.no_book}  # inverse view
        book_yes = {29}
        assert 29 not in {100 - p for p in yes_bids}  # level fully removed
        book._handle_message(delta_msg(seq=4, price="0.3100", delta="200.00"),
                             mono=12.0)
        snap = book.snapshot(TICKER)
        assert snap.yes_bid == 31

    def test_seq_gap_fails_closed_until_resnapshot(self):
        book = make_book()
        book._handle_message(snapshot_msg(seq=2), mono=10.0)
        assert book.snapshot(TICKER) is not None
        book._handle_message(delta_msg(seq=5), mono=11.0)  # 3,4 lost
        assert book.snapshot(TICKER) is None               # trust nothing
        assert book.health(TICKER, mono=11.0).seq_gap
        book._handle_message(snapshot_msg(seq=6), mono=12.0)
        assert book.snapshot(TICKER) is not None           # trust restored
        assert not book.health(TICKER, mono=12.0).seq_gap

    def test_delta_before_snapshot_marks_gap(self):
        book = make_book()
        book._handle_message(delta_msg(seq=1), mono=10.0)
        assert book.snapshot(TICKER) is None
        assert book.health(TICKER, mono=10.0).seq_gap


class TestHealth:
    def test_healthy_after_fresh_snapshot(self):
        book = make_book()
        book._handle_message(snapshot_msg(), mono=10.0)
        h = book.health(TICKER, mono=11.0)
        assert h.healthy and h.subscribed and h.connected
        assert h.last_update_age_s == 1.0

    def test_stale_book_unhealthy(self):
        book = make_book()
        book._handle_message(snapshot_msg(), mono=10.0)
        assert not book.health(TICKER, mono=10.0 + STALE_BOOK_S + 1).healthy

    def test_disconnect_resets_snapshot_trust(self):
        book = make_book()
        book._handle_message(snapshot_msg(), mono=10.0)
        book._mark_disconnected()
        assert book.snapshot(TICKER) is None  # must re-snapshot on reconnect
        assert not book.health(TICKER, mono=10.5).healthy

    def test_unsubscribed_ticker_reports_unsubscribed(self):
        book = make_book(subscribed=False)
        h = book.health(TICKER, mono=1.0)
        assert not h.subscribed and not h.healthy


class TestBrti:
    def test_brti_tick_parsed(self):
        # verbatim live capture, demo WS, 2026-07-23: `data` is a
        # JSON-ENCODED STRING (not an object) and the tick timestamp is
        # body-level `received_at` (ms), not a `ts_ms` field anywhere.
        book = make_book(subscribed=False)
        book._handle_message({
            "type": "cfbenchmarks_value", "sid": 1, "seq": 1, "msg": {
                "index_id": "BRTI", "received_at": 1784775705108,
                "data": ('{"type":"value","time":1784775705000,'
                        '"id":"BRTI","value":"65766.03"}'),
                "avg_60s_data": {"value": "65766.03000000", "window_size": 0,
                                "window_start_ts_ms": 1784775645000,
                                "window_end_ts_exclusive": 1784775705000},
            }})
        b = book.brti()
        assert b is not None
        assert b.value == 65766.03
        assert b.avg_60s == 65766.03
        assert b.settlement_forming is None  # absent outside the final minute
        assert b.ts == datetime.fromtimestamp(1784775705108 / 1e3, tz=timezone.utc)

    def test_brti_settlement_forming_present_in_final_minute(self):
        # documented shape (asyncapi.yaml); not yet observed live since no
        # capture window landed in a settlement final-minute
        book = make_book(subscribed=False)
        book._handle_message({"type": "cfbenchmarks_value", "sid": 1, "msg": {
            "index_id": "BRTI", "received_at": 1784775705108,
            "data": '{"value":"65766.03"}',
            "avg_60s_data": {"value": "65766.03"},
            "last_60s_windowed_average_15min": {"value": "65760.00"},
        }})
        assert book.brti().settlement_forming == 65760.00

    def test_brti_malformed_data_string_does_not_crash(self):
        book = make_book(subscribed=False)
        book._handle_message({"type": "cfbenchmarks_value", "sid": 1, "msg": {
            "index_id": "BRTI", "received_at": 1784775705108,
            "data": "not json", "avg_60s_data": {"value": "1.0"},
        }})
        b = book.brti()
        assert b is not None and b.value is None  # degrades, never raises

    def test_brti_absent_before_first_tick(self):
        assert make_book(subscribed=False).brti() is None


class TestWatchdogRecovery:
    """Regression, found live 2026-07-30: a subscribe sent at window open
    got no snapshot (raced the market's WS availability or the cmd errored)
    and the book sat connected+subscribed with no data for the whole window
    while REST showed a 1c-spread market. Nothing retried — re-snapshots
    only fired on delta seq gaps, which need deltas to arrive at all."""

    def test_never_snapshotted_state_is_selected_after_grace(self):
        book = make_book()
        # immediately after subscribe: inside the pacing window, leave alone
        assert book._states_needing_recovery(mono=0.0) == []
        st = book._books[TICKER]
        st.last_recover_mono = 0.0
        picked = book._states_needing_recovery(mono=STALE_BOOK_S + 0.1)
        assert [s.market.market_ticker for s in picked] == [TICKER]

    def test_selection_repaces_itself(self):
        book = make_book()
        book._books[TICKER].last_recover_mono = 0.0
        assert book._states_needing_recovery(mono=STALE_BOOK_S + 1)
        # picked once -> paced out until another full interval passes
        assert book._states_needing_recovery(mono=STALE_BOOK_S + 2) == []
        assert book._states_needing_recovery(mono=2 * STALE_BOOK_S + 2)

    def test_quiet_book_with_snapshot_is_never_recovered(self):
        """A held snapshot with no deltas is thin-market quiet, not broken —
        recovering it would defeat the staleness self-throttle."""
        book = make_book()
        book._handle_message(snapshot_msg(), mono=0.0)
        book._books[TICKER].last_recover_mono = 0.0
        assert book._states_needing_recovery(mono=10 * STALE_BOOK_S) == []

    def test_stuck_seq_gap_is_recovered(self):
        book = make_book()
        book._handle_message(snapshot_msg(seq=2), mono=0.0)
        book._handle_message(delta_msg(seq=9), mono=1.0)  # gap
        book._books[TICKER].last_recover_mono = 0.0
        picked = book._states_needing_recovery(mono=STALE_BOOK_S + 1)
        assert [s.market.market_ticker for s in picked] == [TICKER]

    def test_disconnected_defers_to_reconnect_resubscribe(self):
        book = make_book(connected=False)
        book._books[TICKER].last_recover_mono = 0.0
        assert book._states_needing_recovery(mono=STALE_BOOK_S + 1) == []
