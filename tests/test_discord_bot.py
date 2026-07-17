import threading
import time

import pytest

from kalshi_bots.skills.discord_bot import ConsoleTransport, DiscordBot
from kalshi_bots.skills.risk_management import RiskManager
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import MarketRef, SizingResult, TradeCard


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
    m = MarketRef(league="mlb", series_ticker="S", event_ticker="E",
                  market_ticker="E-NYY", yes_team_kalshi_abbr="NYY", title="",
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
