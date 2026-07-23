import pytest

from kalshi_bots.skills.discord_bot import ConsoleTransport, DiscordBot
from kalshi_bots.skills.risk_management import RiskManager
from kalshi_bots.skills.vault import Vault


class FakeKalshi:
    def get_balance(self):
        return 1

    def get_positions(self):
        return []


def make_deps(tmp_path):
    (tmp_path / "v" / "03-market-context").mkdir(parents=True)
    vault = Vault(root=str(tmp_path / "v"))
    return RiskManager(vault, FakeKalshi()), vault


def test_autonomous_allowed_on_prod(tmp_path, monkeypatch):
    """Owner decision 2026-07-22: superseded the 2026-07-17 demo-only
    restriction by re-answering the execution-mode question in code —
    autonomous (no per-trade approval) is now authorized on prod too."""
    monkeypatch.setenv("KALSHI_ENV", "prod")
    risk, vault = make_deps(tmp_path)
    bot = DiscordBot(risk, vault, transport=ConsoleTransport(), mode="autonomous")
    assert bot.mode == "autonomous"


def test_autonomous_allowed_on_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "demo")
    risk, vault = make_deps(tmp_path)
    bot = DiscordBot(risk, vault, transport=ConsoleTransport(), mode="autonomous")
    assert bot.mode == "autonomous"


def test_manual_approve_fine_anywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "prod")
    risk, vault = make_deps(tmp_path)
    bot = DiscordBot(risk, vault, transport=ConsoleTransport(), mode="manual_approve")
    assert bot.mode == "manual_approve"


# --- sprint-5 live-trading guard: flag AND typed confirmation AND a
# confirmed-status skill; any single missing -> hard exit ---

from kalshi_bots.orchestrator import LIVE_FLAG, live_trading_guard  # noqa: E402

CONFIRMED_SKILL = {
    "skill": "btc-15min-fair-value", "families": ["KXBTC15M"],
    "signal_types": ["fair-value-candidate"], "market_conditions": ["live"],
    "confidence_threshold": 0.6, "risk_profile": "medium",
    "win_rate": 0.6, "sample_size": 40, "status": "confirmed",
    "last_updated": "2026-07-22",
}


def guard_vault(tmp_path, status=None):
    (tmp_path / "gv" / "02-trading-skills").mkdir(parents=True)
    v = Vault(root=str(tmp_path / "gv"))
    if status:
        fm = dict(CONFIRMED_SKILL, status=status)
        v.write_note("02-trading-skills/btc-15min-fair-value.md", fm, "# s",
                     caller="admin")
    return v


class TestLiveTradingGuard:
    def test_demo_passes_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "demo")
        assert live_trading_guard(guard_vault(tmp_path)) == "demo"

    def test_prod_without_exec_mode_live_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "prod")
        monkeypatch.delenv("EXEC_MODE", raising=False)
        with pytest.raises(SystemExit, match="EXEC_MODE=live not set"):
            live_trading_guard(guard_vault(tmp_path))

    def test_missing_flag_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "prod")
        monkeypatch.setenv("EXEC_MODE", "live")
        with pytest.raises(SystemExit, match="missing the"):
            live_trading_guard(guard_vault(tmp_path, "confirmed"), argv=[])

    def test_missing_confirmation_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "prod")
        monkeypatch.setenv("EXEC_MODE", "live")
        with pytest.raises(SystemExit, match="confirmation not given"):
            live_trading_guard(guard_vault(tmp_path, "confirmed"),
                               argv=[LIVE_FLAG], confirm_input=lambda p: "yes")

    def test_no_confirmed_skill_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "prod")
        monkeypatch.setenv("EXEC_MODE", "live")
        with pytest.raises(SystemExit, match="no confirmed-status"):
            live_trading_guard(guard_vault(tmp_path, "draft"),
                               argv=[LIVE_FLAG],
                               confirm_input=lambda p: "TRADE LIVE")

    def test_all_three_gates_pass(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "prod")
        monkeypatch.setenv("EXEC_MODE", "live")
        mode = live_trading_guard(guard_vault(tmp_path, "confirmed"),
                                  argv=[LIVE_FLAG],
                                  confirm_input=lambda p: "TRADE LIVE")
        assert mode == "live"
