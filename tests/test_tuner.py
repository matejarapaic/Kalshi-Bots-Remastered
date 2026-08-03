import pytest

from kalshi_bots.agents.tuner import Tuner, STATE_PATH
from kalshi_bots.skills import risk_management as rm
from kalshi_bots.skills import tuner as tuner_skill
from kalshi_bots.skills.tuner import (
    LOSS_STREAK_TRIGGER, NO_TRADE_STREAK_TRIGGER, SIGMA_LOSS_STREAK_TRIGGER,
    SIGMA_SESSION_WINDOWS, TunerState, apply_feedback, record_window,
    relax_all, tighten_all, tighten_sigma_floor,
)
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import PostmortemReport

FV = "btc-15min-fair-value"


def report(status="settled", trades=1, pnl=-100, drift=False, vol=None):
    return PostmortemReport(
        family="KXBTC15M", event_id="E", trades_audited=trades,
        entry_violations=0, exit_deviations=0, declined_candidates=0,
        counterfactual_pnl_cents=0, realized_pnl_cents=pnl,
        settlement_status=status, threshold_flags=[], note_path="p",
        constituent_drift=drift, realized_vol=vol)


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


class TestSigmaFloor:
    """Owner-directed 2026-08-02: SIGMA_PLAUSIBLE_MIN moves only through
    tighten_sigma_floor — 2 consecutive losses raise it to the session's
    average realized vol, raise-only, corridor-capped. Never relaxed."""

    def test_fires_at_two_losses_not_one(self):
        s = TunerState()
        # session average (0.25) above the 0.18 baseline so a raise is due
        assert apply_feedback(s, report(pnl=-1, vol=0.25)) == []
        assert not rm.has_override("SIGMA_PLAUSIBLE_MIN")
        adjustments = apply_feedback(s, report(pnl=-1, vol=0.25))
        assert [a.param for a in adjustments] == ["SIGMA_PLAUSIBLE_MIN"]
        assert rm.current("SIGMA_PLAUSIBLE_MIN") == pytest.approx(0.25)

    def test_raises_to_session_average(self):
        s = TunerState()
        apply_feedback(s, report(trades=0, vol=0.30))   # no-trade, still in avg
        apply_feedback(s, report(pnl=-1, vol=0.20))
        apply_feedback(s, report(pnl=-1, vol=0.25))
        assert rm.current("SIGMA_PLAUSIBLE_MIN") == pytest.approx(0.25)  # mean

    def test_raise_only_noop_when_average_below_floor(self):
        s = TunerState()
        apply_feedback(s, report(pnl=-1, vol=0.10))
        adjustments = apply_feedback(s, report(pnl=-1, vol=0.12))
        assert adjustments == []                        # avg 0.11 < floor 0.18
        assert not rm.has_override("SIGMA_PLAUSIBLE_MIN")

    def test_corridor_caps_at_double_baseline(self):
        s = TunerState()
        apply_feedback(s, report(pnl=-1, vol=1.5))
        apply_feedback(s, report(pnl=-1, vol=1.5))
        assert rm.current("SIGMA_PLAUSIBLE_MIN") == pytest.approx(
            2.0 * rm.SIGMA_PLAUSIBLE_MIN)               # not 1.5

    def test_relax_never_touches_sigma_floor(self):
        s = TunerState()
        apply_feedback(s, report(pnl=-1, vol=0.25))
        apply_feedback(s, report(pnl=-1, vol=0.25))
        raised = rm.current("SIGMA_PLAUSIBLE_MIN")
        assert raised > rm.SIGMA_PLAUSIBLE_MIN
        for _ in range(5):
            apply_feedback(s, report(pnl=50, vol=0.25))    # wins relax policy params
        for _ in range(2 * NO_TRADE_STREAK_TRIGGER):
            apply_feedback(s, report(trades=0, vol=0.25))  # no-trade relax too
        assert rm.current("SIGMA_PLAUSIBLE_MIN") == raised  # untouched

    def test_windows_without_vol_reading_do_not_poison_average(self):
        s = TunerState()
        apply_feedback(s, report(pnl=-1, vol=None))
        apply_feedback(s, report(pnl=-1, vol=0.25))
        # only the 0.25 reading counts; None contributed nothing
        assert rm.current("SIGMA_PLAUSIBLE_MIN") == pytest.approx(0.25)

    def test_no_vol_readings_at_all_is_a_noop(self):
        s = TunerState()
        assert tighten_sigma_floor(s, "test") == []
        apply_feedback(s, report(pnl=-1))
        assert apply_feedback(s, report(pnl=-1)) == []

    def test_session_lookback_is_bounded(self):
        s = TunerState()
        for _ in range(SIGMA_SESSION_WINDOWS + 10):
            record_window(s, report(pnl=50, vol=0.2))
        assert len(s.recent_sigmas) == SIGMA_SESSION_WINDOWS

    def test_third_loss_fires_both_sigma_and_policy_tighten(self):
        s = TunerState()
        apply_feedback(s, report(pnl=-1, vol=0.25))
        apply_feedback(s, report(pnl=-1, vol=0.25))      # sigma raise at 2
        adjustments = apply_feedback(s, report(pnl=-1, vol=0.25))  # streak 3
        params = {a.param for a in adjustments}
        assert "MIN_EDGE_CENTS" in params                # policy tighten fired
        assert "SIGMA_PLAUSIBLE_MIN" not in params       # already at avg, no-op


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

    def test_restart_resets_every_parameter_to_baseline(self, vault):
        # Owner-directed 2026-08-02: reset() replaces the old reload()
        # restart-re-apply — persisted overrides are discarded, never replayed.
        tuner = Tuner(vault, env="demo")
        tuner.on_reports([report(pnl=-1) for _ in range(LOSS_STREAK_TRIGGER)])
        assert rm.current("MIN_EDGE_CENTS") > rm.MIN_EDGE_CENTS
        rm.clear_all_overrides()  # simulate process death
        fresh = Tuner(vault, env="demo")
        fresh.reset()
        assert rm.active_overrides() == {}
        assert rm.current("MIN_EDGE_CENTS") == rm.MIN_EDGE_CENTS
        assert fresh.state.loss_streak == 0
        assert fresh.state.windows_seen == 0

    def test_reset_rewrites_the_state_note_clean(self, vault):
        tuner = Tuner(vault, env="demo")
        tuner.on_reports(
            [report(pnl=-1, vol=0.25) for _ in range(LOSS_STREAK_TRIGGER)])
        assert vault.read_note(STATE_PATH).frontmatter["active_overrides"]
        rm.clear_all_overrides()
        fresh = Tuner(vault, env="demo")
        fresh.reset()
        fm = vault.read_note(STATE_PATH).frontmatter
        assert not fm.get("active_overrides")
        assert not fm.get("loss_streak")
        assert not fm.get("recent_sigmas")

    def test_reset_announces_discarded_overrides(self, vault):
        tuner = Tuner(vault, env="demo")
        tuner.on_reports([report(pnl=-1) for _ in range(LOSS_STREAK_TRIGGER)])
        rm.clear_all_overrides()
        discord = FakeDiscord()
        fresh = Tuner(vault, discord=discord, env="demo")
        fresh.reset()
        assert discord.messages
        assert all(level == "warning" for _, level in discord.messages)
        assert "baseline" in discord.messages[0][0]

    def test_reset_with_no_prior_state_is_quiet(self, vault):
        discord = FakeDiscord()
        tuner = Tuner(vault, discord=discord, env="demo")
        tuner.reset()
        assert discord.messages == []
        assert rm.active_overrides() == {}

    def test_sigma_session_memory_does_not_survive_restart(self, vault):
        tuner = Tuner(vault, env="demo")
        tuner.on_reports([report(pnl=50, vol=0.21), report(pnl=50, vol=0.23)])
        rm.clear_all_overrides()
        fresh = Tuner(vault, env="demo")
        fresh.reset()
        assert list(fresh.state.recent_sigmas) == []

    def test_win_relax_notifies_at_info_level(self, vault):
        discord = FakeDiscord()
        tuner = Tuner(vault, discord=discord, env="demo")
        tuner.on_reports([report(pnl=-1) for _ in range(LOSS_STREAK_TRIGGER)])
        discord.messages.clear()
        tuner.on_reports([report(pnl=50)])
        assert discord.messages
        assert all(level == "info" for _, level in discord.messages)
