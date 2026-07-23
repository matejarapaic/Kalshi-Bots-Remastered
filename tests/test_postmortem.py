"""postmortem tests. Spec: skills/postmortem/SKILL.md (sprint-4 cadence:
daily aggregate notes, batched stats, crypto counterfactuals)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bots.skills.postmortem import (
    Postmortem, SettlementMismatch, window_realized_vol,
)
from kalshi_bots.skills.vault import Vault

UTC = timezone.utc
CLOSES = datetime(2026, 7, 23, 1, 30, tzinfo=UTC)
EVENT = "KXBTC15M-26JUL222130"
TICKER = "KXBTC15M-26JUL222130-30"

SKILL_FM = {
    "skill": "btc-15min-fair-value", "families": ["KXBTC15M"],
    "signal_types": ["fair-value-candidate"],
    "market_conditions": ["live", "midpoint"], "confidence_threshold": 0.6,
    "risk_profile": "medium", "win_rate": None, "sample_size": 0,
    "status": "draft", "last_updated": "2026-07-22",
}


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    for d in ("02-trading-skills", "03-market-context/active-windows",
              "04-trade-history/trades", "04-trade-history/postmortems"):
        (root / d).mkdir(parents=True)
    v = Vault(root=str(root))
    v.write_note("02-trading-skills/btc-15min-fair-value.md", dict(SKILL_FM),
                 "# skill", caller="admin")
    return v


def log_lines(spots, healthy=5, total=5, start=None, step_s=30):
    start = start or (CLOSES - timedelta(minutes=15))
    out = []
    for i, s in enumerate(spots):
        ts = (start + timedelta(seconds=i * step_s)).isoformat()
        out.append("- LOG " + json.dumps(
            {"ts": ts, "phase": "midpoint", "spot": s, "sigma": 0.6,
             "healthy": healthy if not isinstance(healthy, list) else healthy[i],
             "total": total, "yes_bid": 50, "yes_ask": 52}))
    return "\n".join(out)


def write_window_note(v, strike=65900.0, body_extra="", spots=None, healthy=5):
    spots = spots if spots is not None else [66000.0 + i for i in range(10)]
    body = f"# Window {TICKER}\n\n## Signals\n{body_extra}\n" + \
        log_lines(spots, healthy=healthy) + "\n"
    v.write_note(f"03-market-context/active-windows/{TICKER}.md", {
        "series_ticker": "KXBTC15M", "event_id": EVENT,
        "market_ticker": TICKER, "strike": strike, "phase": "settled",
    }, body, caller="window-monitor")


def write_trade(v, conditions=None, pnl=None, side="yes", coid="t1", sigma=0.6):
    v.write_note(f"04-trade-history/trades/2026-07-23-{coid}.md", {
        "client_order_id": coid, "event_id": EVENT, "family": "KXBTC15M",
        "market_ticker": TICKER, "skill": "btc-15min-fair-value", "side": side,
        "contracts": 10, "entry_price_cents": 55, "signal_price_cents": 55,
        "fee_cents": 5, "model_prob": 0.72, "sigma": sigma, "spot": 66000.0,
        "strike": 65900.0,
        "entry_conditions": conditions or {"edge_ge_min": True},
        "realized_pnl_cents": pnl, "status": "closed" if pnl is not None else "open",
        "exit_deviation": False, "env": "demo", "signal_id": f"sig-{coid}",
    }, "trade", caller="trader")


def run_pm(vault, result="yes", expiration=66100.0):
    pm = Postmortem(vault, kalshi_client=None)
    return pm.run("KXBTC15M", EVENT, market_result=result,
                  expiration_value=expiration, closes_at=CLOSES)


class TestWindowRealizedVol:
    def test_hand_computed_from_fixture(self):
        # alternating ±r at 30s: population std of sqrt(dt)-normalized
        # returns is r/sqrt(30); annualized by sqrt(31_536_000)
        import math
        r = 3e-4
        p, spots = 66000.0, [66000.0]
        for i in range(20):
            p *= math.exp(r if i % 2 == 0 else -r)
            spots.append(p)
        lines = [json.loads(l[len("- LOG "):]) for l in log_lines(spots).splitlines()]
        vol = window_realized_vol(lines)
        expected = (r / math.sqrt(30)) * math.sqrt(31_536_000)
        assert vol == pytest.approx(expected, rel=1e-6)

    def test_too_few_samples_none(self):
        lines = [json.loads(l[len("- LOG "):])
                 for l in log_lines([66000.0, 66001.0]).splitlines()]
        assert window_realized_vol(lines) is None


class TestAudit:
    def test_settled_win_books_pnl_and_model_hit(self, vault):
        write_window_note(vault)
        write_trade(vault)  # open, side yes; settles yes
        report, outcomes = run_pm(vault, result="yes")
        assert report.settlement_status == "settled"
        assert report.trades_audited == 1
        # held to settlement: 10*100 - (10*55+5) = 445
        assert report.realized_pnl_cents == 445
        assert report.model_direction_hits == 1
        assert outcomes["btc-15min-fair-value"][0]["pnl_cents"] == 445
        note = vault.read_note("04-trade-history/trades/2026-07-23-t1.md")
        assert note.frontmatter["status"] == "closed"
        assert note.frontmatter["exit_reason"] == "held_to_settlement"

    def test_entry_violation_flagged(self, vault):
        write_window_note(vault)
        write_trade(vault, conditions={"edge_ge_min": False}, pnl=100)
        report, _ = run_pm(vault)
        assert report.entry_violations == 1
        note = vault.read_note(report.note_path)
        assert "⚠ ENTRY VIOLATION" in note.body

    def test_settlement_mismatch_raises(self, vault):
        # expiration 66100 >= strike 65900 implies yes; Kalshi said no
        write_window_note(vault, strike=65900.0)
        write_trade(vault, pnl=100)
        with pytest.raises(SettlementMismatch):
            run_pm(vault, result="no", expiration=66100.0)

    def test_pending_settlement(self, vault):
        write_window_note(vault)
        write_trade(vault)  # stays open: no result yet
        report, outcomes = run_pm(vault, result=None, expiration=None)
        assert report.settlement_status == "pending"
        assert outcomes == {}  # open trade has no pnl -> nothing to batch


class TestCryptoCounterfactuals:
    def test_vol_was_wrong_flag(self, vault):
        # violent spot path vs sigma_used 0.6 -> ratio far above 2.0
        import math
        p, spots = 66000.0, [66000.0]
        for i in range(20):
            p *= math.exp(0.002 if i % 2 == 0 else -0.002)
            spots.append(p)
        write_window_note(vault, spots=spots)
        write_trade(vault, pnl=100)
        report, _ = run_pm(vault)
        assert report.vol_ratio is not None and report.vol_ratio > 2.0
        assert any("VOL-WAS-WRONG" in f for f in report.threshold_flags)

    def test_constituent_drift_excludes_from_learning(self, vault):
        healthy = [5, 5, 4, 5, 5, 5, 5, 5, 5, 5]  # one degraded sample
        write_window_note(vault, healthy=healthy)
        write_trade(vault, pnl=100)
        report, outcomes = run_pm(vault)
        assert report.constituent_drift is True
        assert outcomes["btc-15min-fair-value"][0]["excluded"] is True

    def test_model_miss_counted(self, vault):
        write_window_note(vault)
        write_trade(vault, side="yes")
        report, _ = run_pm(vault, result="no", expiration=65000.0)
        assert report.model_direction_hits == 0
        note = vault.read_note(report.note_path)
        assert "model✗" in note.body


class TestDeclined:
    def test_counterfactual_from_signal_log(self, vault):
        sig = ('- SIGNAL {"id": "sig-9", "type": "fair-value-candidate", '
               '"market_ticker": "%s", "side": "yes", "entry_price_cents": 55}'
               % TICKER)
        write_window_note(vault, body_extra=sig + "\n")
        report, _ = run_pm(vault, result="yes")
        assert report.declined_candidates == 1
        # 100 contracts: 100*100 - (100*55 + fee ceil(7*100*55*45/10000)=174)
        assert report.counterfactual_pnl_cents == 10000 - (5500 + 174)

    def test_zero_trades_still_writes_section(self, vault):
        write_window_note(vault)
        report, _ = run_pm(vault)
        note = vault.read_note(report.note_path)
        assert "watched, nothing traded" in note.body


class TestDailyAggregate:
    def test_two_windows_share_one_note(self, vault):
        write_window_note(vault)
        write_trade(vault, pnl=100)
        pm = Postmortem(vault, kalshi_client=None)
        r1, _ = pm.run("KXBTC15M", EVENT, market_result="yes",
                       expiration_value=66100.0, closes_at=CLOSES)
        r2, _ = pm.run("KXBTC15M", "KXBTC15M-26JUL222145",
                       market_result="no", expiration_value=65000.0,
                       closes_at=CLOSES + timedelta(minutes=15))
        assert r1.note_path == r2.note_path
        note = vault.read_note(r1.note_path)
        assert note.frontmatter["windows"] == 2
        assert note.frontmatter["settled_events"] == [
            EVENT, "KXBTC15M-26JUL222145"]
        assert f"## {EVENT} — yes" in note.body
        assert "## KXBTC15M-26JUL222145 — no" in note.body

    def test_rerun_is_idempotent(self, vault):
        write_window_note(vault)
        write_trade(vault, pnl=100)
        pm = Postmortem(vault, kalshi_client=None)
        pm.run("KXBTC15M", EVENT, market_result="yes",
               expiration_value=66100.0, closes_at=CLOSES)
        pm.run("KXBTC15M", EVENT, market_result="yes",
               expiration_value=66100.0, closes_at=CLOSES)
        note = vault.read_note(pm._daily_path("KXBTC15M", CLOSES))
        assert note.frontmatter["windows"] == 1  # not double-counted


class TestBatchedStats:
    def outcomes(self, n_wins, n_losses, excluded=False):
        rows = [{"pnl_cents": 100, "entry_price_cents": 55, "excluded": excluded}
                for _ in range(n_wins)]
        rows += [{"pnl_cents": -50, "entry_price_cents": 55, "excluded": excluded}
                 for _ in range(n_losses)]
        return {"btc-15min-fair-value": rows}

    def test_batch_updates_demo_stats_prod_untouched(self, vault):
        pm = Postmortem(vault, kalshi_client=None, env="demo")
        pm.update_skill_stats(self.outcomes(3, 1))
        fm = vault.read_note("02-trading-skills/btc-15min-fair-value.md").frontmatter
        assert fm["demo_sample_size"] == 4 and fm["demo_win_rate"] == 0.75
        assert fm["sample_size"] == 0 and fm["win_rate"] is None

    def test_accumulates_across_batches(self, vault):
        pm = Postmortem(vault, kalshi_client=None)
        pm.update_skill_stats(self.outcomes(1, 1))
        pm.update_skill_stats(self.outcomes(2, 0))
        fm = vault.read_note("02-trading-skills/btc-15min-fair-value.md").frontmatter
        assert fm["demo_sample_size"] == 4
        assert fm["demo_win_rate"] == pytest.approx(0.75)

    def test_excluded_outcomes_do_not_count(self, vault):
        pm = Postmortem(vault, kalshi_client=None)
        pm.update_skill_stats(self.outcomes(3, 1, excluded=True))
        fm = vault.read_note("02-trading-skills/btc-15min-fair-value.md").frontmatter
        assert fm.get("demo_sample_size") in (None, 0)

    def test_threshold_review_flag(self, vault):
        pm = Postmortem(vault, kalshi_client=None)
        flags = pm.update_skill_stats(self.outcomes(20, 0))  # wr 1.0 vs be 0.55
        assert any("THRESHOLD REVIEW" in f for f in flags)
