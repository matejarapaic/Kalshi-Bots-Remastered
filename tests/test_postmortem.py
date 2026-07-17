import pytest

from kalshi_bots.skills.postmortem import Postmortem, SettlementMismatch
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import Settlement

SKILL_FM = {
    "skill": "garbage-time-mispricing", "sports": ["mlb"],
    "market_conditions": ["live"], "confidence_threshold": 0.6,
    "risk_profile": "low", "win_rate": None, "sample_size": 0,
    "status": "confirmed", "last_updated": "2026-07-17",
}


class FakeKalshi:
    def __init__(self, result="yes"):
        self.result = result

    def get_settlements(self, ticker):
        if self.result is None:
            return []
        return [Settlement(market_ticker=ticker, result=self.result,
                           settled_ts=None, revenue_cents=0, raw={})]


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    for d in ("02-trading-skills", "03-market-context/active-games",
              "04-trade-history/trades", "04-trade-history/postmortems"):
        (root / d).mkdir(parents=True)
    v = Vault(root=str(root))
    v.write_note("02-trading-skills/garbage-time-mispricing.md", dict(SKILL_FM),
                 "# skill", caller="admin")
    return v


def write_game_note(v, winner="NYY", body_extra=""):
    v.write_note("03-market-context/active-games/mlb-e1.md", {
        "league": "mlb", "espn_event_id": "e1", "status": "final",
        "market_ticker": "M-NYY", "yes_team_kalshi_abbr": "NYY",
        "winner_kalshi_abbr": winner,
    }, "# game\n" + body_extra, caller="game-monitor")


def write_trade(v, conditions=None, pnl=None, side="yes"):
    v.write_note("04-trade-history/trades/t1.md", {
        "espn_event_id": "e1", "league": "mlb", "market_ticker": "M-NYY",
        "skill": "garbage-time-mispricing", "side": side, "contracts": 10,
        "entry_price_cents": 93, "fee_cents": 5, "signal_price_cents": 93,
        "entry_conditions": conditions or {"decided": True, "fee_edge": True},
        "realized_pnl_cents": pnl, "status": "closed", "env": "demo",
        "signal_id": "sig-1", "exit_deviation": False,
    }, "trade", caller="trader")


class TestAudit:
    def test_clean_trade_settled_win(self, vault):
        write_game_note(vault)
        write_trade(vault)
        pm = Postmortem(vault, FakeKalshi("yes"))
        r = pm.run("mlb", "e1")
        assert r.settlement_status == "settled"
        assert r.trades_audited == 1 and r.entry_violations == 0
        # pnl derived from settlement: 10*100 - (10*93+5) = 65
        assert r.realized_pnl_cents == 65

    def test_entry_violation_flagged(self, vault):
        write_game_note(vault)
        write_trade(vault, conditions={"decided": True, "fee_edge": False})
        r = Postmortem(vault, FakeKalshi("yes")).run("mlb", "e1")
        assert r.entry_violations == 1
        note = vault.read_note(r.note_path)
        assert "⚠ ENTRY VIOLATION" in note.body

    def test_settlement_mismatch_raises(self, vault):
        write_game_note(vault, winner="NYY")   # ESPN says NYY won -> expect yes
        write_trade(vault)
        pm = Postmortem(vault, FakeKalshi("no"))  # Kalshi settled no
        with pytest.raises(SettlementMismatch):
            pm.run("mlb", "e1")
        note = vault.read_note(pm._note_path("mlb", "e1"))
        assert note.frontmatter["settlement_status"] == "mismatch"

    def test_pending_settlement(self, vault):
        write_game_note(vault)
        write_trade(vault)
        # trade status closed -> pending only matters for open trades; force open
        vault.update_frontmatter("04-trade-history/trades/t1.md",
                                 {"status": "open"}, caller="trader")
        r = Postmortem(vault, FakeKalshi(result=None)).run("mlb", "e1")
        assert r.settlement_status == "pending"


class TestStats:
    def test_demo_stats_updated_prod_untouched(self, vault):
        write_game_note(vault)
        write_trade(vault, pnl=65)
        Postmortem(vault, FakeKalshi("yes"), env="demo").run("mlb", "e1")
        fm = vault.read_note("02-trading-skills/garbage-time-mispricing.md").frontmatter
        assert fm["demo_sample_size"] == 1 and fm["demo_win_rate"] == 1.0
        assert fm["sample_size"] == 0 and fm["win_rate"] is None  # prod untouched

    def test_stats_math_accumulates(self, vault):
        write_game_note(vault)
        vault.update_frontmatter("02-trading-skills/garbage-time-mispricing.md",
                                 {"demo_win_rate": 0.5, "demo_sample_size": 2},
                                 caller="analyst")
        write_trade(vault, pnl=65)  # a win
        Postmortem(vault, FakeKalshi("yes")).run("mlb", "e1")
        fm = vault.read_note("02-trading-skills/garbage-time-mispricing.md").frontmatter
        # (0.5*2 + 1) / 3 = 0.6667
        assert fm["demo_sample_size"] == 3
        assert fm["demo_win_rate"] == pytest.approx(0.6667, abs=1e-3)


class TestDeclined:
    def test_counterfactual_from_signal_log(self, vault):
        sig = ('- SIGNAL {"id": "sig-9", "type": "garbage-time-candidate", '
               '"market_ticker": "M-NYY", "side": "yes", "entry_price_cents": 93, '
               '"declined_reason": "matcher_below_threshold"}')
        write_game_note(vault, body_extra=f"\n## Signals\n\n{sig}\n")
        r = Postmortem(vault, FakeKalshi("yes")).run("mlb", "e1")
        assert r.declined_candidates == 1
        # 100 contracts: 100*100 - (100*93 + fee 46) = 654
        assert r.counterfactual_pnl_cents == 100 * 100 - (9300 + 46)

    def test_zero_trades_still_writes_note(self, vault):
        write_game_note(vault)
        r = Postmortem(vault, FakeKalshi("yes")).run("mlb", "e1")
        note = vault.read_note(r.note_path)
        assert "watched, nothing traded" in note.body


class TestIdempotency:
    def test_second_run_noop(self, vault):
        write_game_note(vault)
        write_trade(vault, pnl=65)
        pm = Postmortem(vault, FakeKalshi("yes"))
        pm.run("mlb", "e1")
        pm.run("mlb", "e1")  # second run short-circuits
        fm = vault.read_note("02-trading-skills/garbage-time-mispricing.md").frontmatter
        assert fm["demo_sample_size"] == 1  # not double-counted
