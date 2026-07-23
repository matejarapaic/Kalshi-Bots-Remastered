import threading
import time

import pytest
import requests

from kalshi_bots.skills.discord_bot import (
    ConsoleTransport, DiscordBot, DiscordTransport, DiscordUnavailable,
)
from kalshi_bots.skills.risk_management import RiskManager
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import MarketRef, SizingResult, TradeCard


class FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.headers = {}
        self.response = response
        self.exc = exc
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if self.exc:
            raise self.exc
        return self.response


class TestDiscordTransport:
    def test_send_success_returns_message_id(self):
        session = FakeSession(response=FakeResponse(200, {"id": "999"}))
        t = DiscordTransport("tok", "chan1", session=session)
        msg_id = t.send({"text": "hello world"})
        assert msg_id == "999"
        assert session.headers["Authorization"] == "Bot tok"
        url, body = session.calls[0]
        assert url == "https://discord.com/api/v10/channels/chan1/messages"
        assert body == {"content": "hello world"}

    def test_truncates_to_discord_max_length(self):
        session = FakeSession(response=FakeResponse(200, {"id": "1"}))
        t = DiscordTransport("tok", "chan1", session=session)
        t.send({"text": "x" * 5000})
        _, body = session.calls[0]
        assert len(body["content"]) == 2000

    def test_rate_limited_raises_unavailable(self):
        session = FakeSession(response=FakeResponse(429, text="slow down"))
        t = DiscordTransport("tok", "chan1", session=session)
        with pytest.raises(DiscordUnavailable):
            t.send({"text": "x"})

    def test_http_error_raises_unavailable(self):
        session = FakeSession(response=FakeResponse(403, text="forbidden"))
        t = DiscordTransport("tok", "chan1", session=session)
        with pytest.raises(DiscordUnavailable):
            t.send({"text": "x"})

    def test_network_failure_raises_unavailable(self):
        session = FakeSession(exc=requests.ConnectionError("no route"))
        t = DiscordTransport("tok", "chan1", session=session)
        with pytest.raises(DiscordUnavailable):
            t.send({"text": "x"})


class FakeKalshi:
    def get_balance(self):
        return 50_000

    def get_positions(self):
        return []


@pytest.fixture
def bot(tmp_path):
    root = tmp_path / "vault"
    (root / "03-market-context").mkdir(parents=True)
    (root / "02-trading-skills").mkdir(parents=True)
    vault = Vault(root=str(root))
    risk = RiskManager(vault, FakeKalshi())
    return DiscordBot(risk, vault, transport=ConsoleTransport(),
                      mode="manual_approve")


def card(coid="c1", live=True):
    m = MarketRef(family="crypto", series_ticker="S", event_ticker="E",
                  market_ticker="E-30", yes_label="up", title="",
                  close_ts=None, settlement_notes=None)
    s = SizingResult(contracts=10, limit_price=60, kelly_fraction_used=0.05,
                     capped_by=[], est_fee_cents_total=17)
    return TradeCard(client_order_id=coid, skill_name="test", market=m,
                     side="yes", action="buy", sizing=s,
                     snapshot={"edge": 0.08}, is_live=live)


class TestApproval:
    def test_approve_flow(self, bot):
        result = {}

        def click():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if bot.resolve_card("c1", "approved", "owner", authorized=True):
                    return
                time.sleep(0.01)

        t = threading.Thread(target=click)
        t.start()
        outcome = bot.send_trade_card(card(), timeout_s=3)
        t.join()
        assert outcome.decision == "approved" and outcome.decided_by == "owner"

    def test_timeout_expires_never_approves(self, bot):
        outcome = bot.send_trade_card(card(), timeout_s=0.05)
        assert outcome.decision == "expired"
        # late click after expiry no-ops
        assert bot.resolve_card("c1", "approved", "owner", authorized=True) is False

    def test_unauthorized_click_noop(self, bot):
        def click():
            time.sleep(0.05)
            assert bot.resolve_card("c1", "approved", "rando", authorized=False) is False

        t = threading.Thread(target=click)
        t.start()
        outcome = bot.send_trade_card(card(), timeout_s=0.2)
        t.join()
        assert outcome.decision == "expired"  # unauthorized never decided it

    def test_double_click_idempotent(self, bot):
        decisions = []

        def clicks():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not decisions:
                if bot.resolve_card("c1", "approved", "owner", authorized=True):
                    decisions.append("first")
                    # second click must be rejected
                    assert bot.resolve_card("c1", "rejected", "owner",
                                            authorized=True) is False
                    return
                time.sleep(0.01)

        t = threading.Thread(target=clicks)
        t.start()
        outcome = bot.send_trade_card(card(), timeout_s=3)
        t.join()
        assert outcome.decision == "approved" and decisions == ["first"]

    def test_outage_fail_closed(self, bot):
        bot.transport.available = False
        outcome = bot.send_trade_card(card(), timeout_s=1)
        assert outcome.decision == "undeliverable"

    def test_autonomous_mode_no_wait(self, bot):
        bot.mode = "autonomous"
        start = time.monotonic()
        outcome = bot.send_trade_card(card())
        assert outcome.decision == "approved"
        assert outcome.decided_by == "autonomous"
        assert time.monotonic() - start < 0.5

    def test_autonomous_mode_sends_no_notify_itself(self, bot):
        """Regression: send_trade_card must not announce a trade before the
        order is actually placed — that used to produce a false-positive
        Discord message for orders that ended up unfilled. The real
        notification is the trader's job, sent only after a confirmed fill."""
        bot.mode = "autonomous"
        bot.send_trade_card(card())
        assert bot.transport.sent == []


class TestQueue:
    def test_overflow_drops_oldest_droppable(self, bot):
        bot.transport.available = False
        for i in range(205):
            bot._enqueue({"kind": "notify", "text": str(i), "droppable": True})
        texts = [m["text"] for m in bot._queue]
        assert len(bot._queue) == 200
        assert "0" not in texts and "204" in texts

    def test_critical_never_dropped(self, bot):
        bot.transport.available = False
        for i in range(200):
            bot._enqueue({"kind": "notify", "text": str(i), "droppable": True})
        bot._enqueue({"kind": "notify", "text": "CRIT", "droppable": False},
                     critical=True)
        assert any(m["text"] == "CRIT" for m in bot._queue)

    def test_flush_requeues_on_outage(self, bot):
        bot.transport.available = False
        bot._enqueue({"kind": "notify", "text": "x", "droppable": True})
        bot.flush()
        assert len(bot._queue) == 1
        bot.transport.available = True
        bot.flush()
        assert len(bot._queue) == 0


class TestCommands:
    def test_halt_persists(self, bot, tmp_path):
        assert "halted" in bot.handle_command("halt", "testing")
        assert bot.risk.halted()[0] is True
        # restart: new RiskManager over same vault
        rm2 = RiskManager(bot.vault, FakeKalshi())
        assert rm2.halted()[0] is True
        bot.handle_command("resume")
        assert bot.risk.halted()[0] is False

    def test_positions_and_pnl(self, bot):
        assert "open positions: 0" in bot.handle_command("positions")
        assert "daily realized" in bot.handle_command("pnl")

    def test_unknown(self, bot):
        assert "unknown command" in bot.handle_command("dance")

    def test_window_no_note_yet(self, bot):
        assert bot.handle_command("window") == "no active window note yet"

    def test_window_shows_most_recently_updated(self, bot):
        (bot.vault._abs("03-market-context/active-windows")).mkdir(parents=True)
        bot.vault.write_note(
            "03-market-context/active-windows/KXBTC15M-1.md",
            {"market_ticker": "KXBTC15M-1", "phase": "settled", "strike": 100.0,
             "spot": 101.0, "sigma": 0.5, "updated": "2026-07-23T01:00:00+00:00"},
            "# w1\n", caller="window-monitor")
        bot.vault.write_note(
            "03-market-context/active-windows/KXBTC15M-2.md",
            {"market_ticker": "KXBTC15M-2", "phase": "midpoint", "strike": 200.0,
             "spot": 202.0, "sigma": 0.6, "updated": "2026-07-23T01:15:00+00:00"},
            "# w2\n", caller="window-monitor")
        out = bot.handle_command("window")
        assert "KXBTC15M-2" in out and "midpoint" in out
        assert "KXBTC15M-1" not in out
