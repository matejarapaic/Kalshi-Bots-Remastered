import pytest

from kalshi_bots.agents.tuner import Tuner, STATE_PATH
from kalshi_bots.skills import risk_management as rm
from kalshi_bots.skills import tuner as tuner_skill
from kalshi_bots.skills.tuner import (
    LOSS_STREAK_TRIGGER, NO_TRADE_STREAK_TRIGGER, TunerState,
    apply_feedback, record_window, relax_all, tighten_all,
)
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import PostmortemReport

FV = "btc-15min-fair-value"


def report(status="settled", trades=1, pnl=-100, drift=False):
    return PostmortemReport(
        family="KXBTC15M", event_id="E", trades_audited=trades,
        entry_violations=0, exit_deviations=0, declined_candidates=0,
        counterfactual_pnl_cents=0, realized_pnl_cents=pnl,
        settlement_status=status, threshold_flags=[], note_path="p",
        constituent_drift=drift)


class TestStreakCounting:
    def test_classification(self):
        s = TunerState()
        assert record_window(s, report(pnl=-100)) == "loss"
        assert record_window(s, report(pnl=200)) == "win"
        assert record_window(s, report(trades=0, pnl=0)) == "no_trade"
        assert record_window(s, report(status="pending")) == "skipped"

    def test_drift_windows_count_toward_streaks(self):
        # 2026-07-31 fix: constituent_drift excludes a window from win_rate
        # learning (postmortem's concern), NOT from streak counting — real
        # fills and settlements are valid outcome data even when our own
        # feed blipped. The old skip zeroed the streaks permanently live.
        s = TunerState()
        assert record_window(s, report(pnl=-100, drift=True)) == "loss"
        assert record_window(s, report(trades=0, drift=True)) == "no_trade"
        assert s.loss_streak == 1
        assert s.no_trade_streak == 1
        assert s.windows_seen == 2

    def test_loss_streak_counts_and_win_resets(self):
        s = TunerState()
        record_window(s, report(pnl=-1))
        record_window(s, report(pnl=-1))
        assert s.loss_streak == 2
        record_window(s, report(pnl=50))
        assert s.loss_streak == 0

    def test_no_trade_does_not_reset_loss_streak(self):
        s = TunerState()
        record_window(s, report(pnl=-1))
        record_window(s, report(trades=0))
        assert s.loss_streak == 1
        assert s.no_trade_streak == 1

    def test_traded_window_resets_no_trade_streak(self):
        s = TunerState()
        record_window(s, report(trades=0))
        record_window(s, report(trades=0))
        assert s.no_trade_streak == 2
        record_window(s, report(pnl=-1))
        assert s.no_trade_streak == 0

    def test_skipped_windows_touch_nothing(self):
        s = TunerState()
        record_window(s, report(pnl=-1))
        record_window(s, report(status="voided"))
        record_window(s, report(status="pending"))
        assert s.loss_streak == 1
        assert s.windows_seen == 1


class TestTighten:
    def test_fires_only_at_trigger_multiples(self):
        s = TunerState()
        for i in range(1, LOSS_STREAK_TRIGGER):
            assert apply_feedback(s, report(pnl=-1)) == []
        adjustments = apply_feedback(s, report(pnl=-1))  # streak hits 3
        assert adjustments
        # streak 4, 5: nothing; streak 6: another round
        assert apply_feedback(s, report(pnl=-1)) == []
        assert apply_feedback(s, report(pnl=-1)) == []
        assert apply_feedback(s, report(pnl=-1)) != []

    def test_one_round_steps_every_policy_param(self):
        adjustments = tighten_all("test")
        by_param = {(a.param, a.skill): a for a in adjustments}
        assert by_param[("MIN_EDGE_CENTS", None)].new_value == 5     # 4 + 1
        assert by_param[("PER_TRADE_CAP_PCT", FV)].new_value == 4    # round(5*.85)
        assert by_param[("MAX_CONTRACTS_PER_WINDOW", None)].new_value == 17
        assert by_param[("SKILL_RISK_MULTIPLIER", FV)].new_value == (0.85, 0.85)
        assert by_param[("STOP_LOSS_PCT", None)].new_value == 42
        assert rm.current("MIN_EDGE_CENTS") == 5

    def test_corridor_floors_hold_under_repeated_tightening(self):
        for _ in range(50):
            tighten_all("test")
        assert rm.current("MIN_EDGE_CENTS") == 2 * rm.MIN_EDGE_CENTS  # ceiling
        assert rm.current("PER_TRADE_CAP_PCT", skill=FV) >= 1
        assert rm.current("MAX_CONTRACTS_PER_WINDOW") >= 5   # floor(0.25*20)
        assert rm.current("MAX_OPEN_POSITIONS") >= 1
        assert rm.current("STOP_LOSS_PCT") >= 25
        assert rm.current("TOTAL_EXPOSURE_CAP_PCT") >= 7
        mult = rm.current("SKILL_RISK_MULTIPLIER", skill=FV)
        assert all(v >= 0.25 for v in mult)
        assert tighten_all("test") == []  # everything pinned at its edge


class TestRelax:
    """Owner-directed 2026-07-30: relax is unbounded past baseline (see
    skills/tuner/SKILL.md). These tests assert the new behavior, replacing
    the prior "relax never crosses baseline" guarantee."""

    def test_win_relaxes_past_baseline_without_clearing(self):
        s = TunerState()
        for _ in range(LOSS_STREAK_TRIGGER):
            apply_feedback(s, report(pnl=-1))
        assert rm.current("MIN_EDGE_CENTS") == 5          # tightened once: 4 + 1
        for _ in range(3):
            apply_feedback(s, report(pnl=50))
        assert rm.current("MIN_EDGE_CENTS") == 2           # 5->4->3->2, past baseline (4)
        assert rm.has_override("MIN_EDGE_CENTS")            # never cleared, keeps moving

    def test_relax_keeps_moving_past_baseline_floored_by_domain_only(self):
        for _ in range(10):
            relax_all("test")
        # MIN_EDGE_CENTS can't go negative (no such thing as requiring less
        # than zero edge) -- that's the domain floor, not a baseline clamp.
        assert rm.current("MIN_EDGE_CENTS") == 0
        adjustments = relax_all("test")
        assert not any(a.param == "MIN_EDGE_CENTS" for a in adjustments)  # pinned
        # Sizing/exposure params have no ceiling at all -- they keep growing.
        assert rm.current("PER_TRADE_CAP_PCT", skill=FV) > rm.PER_TRADE_CAP_PCT[FV]

    def test_no_trade_streak_relaxes_at_trigger_multiple_only(self):
        s = TunerState()
        for _ in range(LOSS_STREAK_TRIGGER):
            apply_feedback(s, report(pnl=-1))
        before = dict(rm.active_overrides())
        for _ in range(NO_TRADE_STREAK_TRIGGER - 1):
            assert apply_feedback(s, report(trades=0)) == []
        assert rm.active_overrides() == before   # untouched until the trigger
        assert apply_feedback(s, report(trades=0)) != []  # Nth (trigger) fires

    def test_relax_fires_even_with_no_prior_tightening(self):
        s = TunerState()
        assert rm.active_overrides() == {}
        adjustments = apply_feedback(s, report(pnl=50))
        assert adjustments                                  # no longer a no-op
        assert rm.current("MIN_EDGE_CENTS") < rm.MIN_EDGE_CENTS


class FakeDiscord:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def notify(self, text, level="info"):
        self.messages.append((text, level))


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "03-market-context").mkdir(parents=True)
    return Vault(root=str(root))


class TestAgent:
    def test_on_reports_adjusts_persists_and_notifies(self, vault):
        discord = FakeDiscord()
        tuner = Tuner(vault, discord=discord, env="demo")
        losses = [report(pnl=-1) for _ in range(LOSS_STREAK_TRIGGER)]
        adjustments = tuner.on_reports(losses)
        assert adjustments
        assert all(level == "warning" for _, level in discord.messages)
        note = vault.read_note(STATE_PATH)
        assert note.frontmatter["loss_streak"] == LOSS_STREAK_TRIGGER
        assert note.frontmatter["active_overrides"]

    def test_restart_round_trip_reapplies_overrides(self, vault):
        tuner = Tuner(vault, env="demo")
        tuner.on_reports([report(pnl=-1) for _ in range(LOSS_STREAK_TRIGGER)])
        edge_before = rm.current("MIN_EDGE_CENTS")
        assert edge_before > rm.MIN_EDGE_CENTS
        rm.clear_all_overrides()  # simulate process death
        fresh = Tuner(vault, env="demo")
        fresh.reload()
        assert fresh.state.loss_streak == LOSS_STREAK_TRIGGER
        assert rm.current("MIN_EDGE_CENTS") == edge_before

    def test_reload_drops_overrides_the_corridor_no_longer_accepts(
            self, vault, monkeypatch):
        tuner = Tuner(vault, env="demo")
        tuner.on_reports([report(pnl=-1) for _ in range(LOSS_STREAK_TRIGGER)])
        rm.clear_all_overrides()
        # baseline lowered since state was persisted: the stored tighten-side
        # MIN_EDGE override (5) now exceeds the new tighten ceiling
        # (2x baseline = 4) and must be dropped. (Relax-side values have no
        # ceiling since 2026-07-30, so only a tighten-side violation like
        # this one still triggers a drop-on-reload.)
        monkeypatch.setattr(
            "kalshi_bots.skills.risk_management.MIN_EDGE_CENTS", 2)
        fresh = Tuner(vault, env="demo")
        fresh.reload()
        assert not rm.has_override("MIN_EDGE_CENTS")
        assert rm.current("MIN_EDGE_CENTS") == 2

    def test_win_relax_notifies_at_info_level(self, vault):
        discord = FakeDiscord()
        tuner = Tuner(vault, discord=discord, env="demo")
        tuner.on_reports([report(pnl=-1) for _ in range(LOSS_STREAK_TRIGGER)])
        discord.messages.clear()
        tuner.on_reports([report(pnl=50)])
        assert discord.messages
        assert all(level == "info" for _, level in discord.messages)
