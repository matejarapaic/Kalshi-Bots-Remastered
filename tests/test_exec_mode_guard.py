import pytest

from kalshi_bots.skills.discord_bot import ConsoleTransport, DiscordBot, DiscordError
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


def test_autonomous_refused_on_prod(tmp_path, monkeypatch):
    """Owner decision 2026-07-17: autonomous is demo-only; there is no
    override env var — prod autonomy requires re-answering the question."""
    monkeypatch.setenv("KALSHI_ENV", "prod")
    risk, vault = make_deps(tmp_path)
    with pytest.raises(DiscordError, match="demo only"):
        DiscordBot(risk, vault, transport=ConsoleTransport(), mode="autonomous")


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
