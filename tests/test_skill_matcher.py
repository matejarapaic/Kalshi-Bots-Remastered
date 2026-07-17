from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bots.skills.skill_matcher import (
    SkillMatcher, W_FRESH, W_HIST, W_SIGNAL, W_TAGS, derive_condition_tags,
)
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import CandidateSignal, GameState, TeamRef

NOW = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)

SKILL_FM = {
    "skill": "garbage-time-mispricing", "sports": ["nfl", "nba", "mlb"],
    "market_conditions": ["live", "blowout", "endgame"],
    "confidence_threshold": 0.6, "risk_profile": "low",
    "win_rate": None, "sample_size": 0, "status": "confirmed",
    "last_updated": "2026-07-17",
}


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "02-trading-skills").mkdir(parents=True)
    v = Vault(root=str(root))
    v.write_note("02-trading-skills/garbage-time-mispricing.md", dict(SKILL_FM),
                 "# body", caller="admin")
    return v


def game(league="mlb", status="in_progress", hs=9, as_=0, period=9,
         clock=None, fetched=NOW):
    return GameState(league=league, espn_event_id="e1", status=status,
                     home=TeamRef(league, "BOS", None, "Boston Red Sox"),
                     away=TeamRef(league, "TB", None, "Tampa Bay Rays"),
                     home_score=hs, away_score=as_, period=period,
                     period_half=None, clock_seconds=clock, win_prob_home=0.99,
                     win_prob_source_ts=None, start_time=NOW - timedelta(hours=2),
                     fetched_at=fetched)


def signal(sig_type="garbage-time-candidate", payload=None):
    return CandidateSignal(signal_type=sig_type, league="mlb",
                           espn_event_id="e1", market_ticker="T",
                           payload=payload or {}, emitted_at=NOW)


class TestTags:
    def test_live_blowout_endgame_mlb(self):
        tags = derive_condition_tags(game(), signal(), NOW)
        assert set(tags) == {"live", "blowout", "endgame"}

    def test_pregame_window(self):
        g = game(status="scheduled")
        g.start_time = NOW + timedelta(minutes=30)
        assert "pregame" in derive_condition_tags(g, signal(), NOW)
        g.start_time = NOW + timedelta(minutes=90)
        assert "pregame" not in derive_condition_tags(g, signal(), NOW)

    def test_blowout_league_margins(self):
        assert "blowout" in derive_condition_tags(
            game(league="nba", hs=100, as_=85, period=3), signal(), NOW)
        assert "blowout" not in derive_condition_tags(
            game(league="nba", hs=100, as_=86, period=3), signal(), NOW)
        assert "blowout" in derive_condition_tags(
            game(league="nfl", hs=27, as_=10, period=3, clock=900), signal(), NOW)

    def test_endgame_rules(self):
        assert "endgame" in derive_condition_tags(
            game(league="nba", period=4, clock=299), signal(), NOW)
        assert "endgame" not in derive_condition_tags(
            game(league="nba", period=4, clock=301), signal(), NOW)
        assert "endgame" in derive_condition_tags(game(period=8), signal(), NOW)

    def test_swing_and_injury_tags(self):
        tags = derive_condition_tags(
            game(), signal(payload={"swing": {"magnitude": 0.16}}), NOW)
        assert "momentum-swing" in tags and "high-volatility" in tags
        tags2 = derive_condition_tags(
            game(), signal(payload={"swing": {"magnitude": 0.05}}), NOW)
        assert "momentum-swing" in tags2 and "high-volatility" not in tags2
        assert "news-event" in derive_condition_tags(
            game(), signal(payload={"injury": {"player": "x"}}), NOW)


class TestGates:
    def test_draft_skill_invisible(self, vault):
        vault.update_frontmatter("02-trading-skills/garbage-time-mispricing.md",
                                 {"status": "draft"}, caller="admin")
        assert SkillMatcher(vault).match(signal(), game(), now=NOW) == []

    def test_league_scope(self, vault):
        vault.update_frontmatter("02-trading-skills/garbage-time-mispricing.md",
                                 {"sports": ["nba"]}, caller="admin")
        assert SkillMatcher(vault).match(signal(), game(), now=NOW) == []

    def test_signal_type_mapping(self, vault):
        assert SkillMatcher(vault).match(
            signal("overreaction-candidate"), game(), now=NOW) == []
        assert SkillMatcher(vault).match(signal(), game(), now=NOW)

    def test_game_final_never_matches(self, vault):
        assert SkillMatcher(vault).match(signal("game-final"), game(), now=NOW) == []

    def test_tag_overlap_gate(self, vault):
        g = game(status="scheduled", hs=0, as_=0, period=0)  # no tag overlap
        g.start_time = NOW + timedelta(hours=5)
        assert SkillMatcher(vault).match(signal(), g, now=NOW) == []

    def test_invalid_threshold_excluded(self, vault, tmp_path):
        # bypass vault validation by writing the file directly (simulates human error)
        p = tmp_path / "vault" / "02-trading-skills" / "garbage-time-mispricing.md"
        text = p.read_text().replace("confidence_threshold: 0.6",
                                     "confidence_threshold: 1.6")
        p.write_text(text)
        v = Vault(root=str(tmp_path / "vault"))
        assert SkillMatcher(v).match(signal(), game(), now=NOW) == []


class TestScoring:
    def test_deterministic_and_decomposed(self, vault):
        m = SkillMatcher(vault)
        r1 = m.match(signal(), game(), now=NOW)
        r2 = m.match(signal(), game(), now=NOW)
        assert r1[0].score == r2[0].score
        # 3/3 tags, fresh, neutral history
        expected = W_SIGNAL * 1.0 + W_TAGS * 1.0 + W_FRESH * 1.0 + W_HIST * 0.5
        assert r1[0].score == pytest.approx(expected)
        assert r1[0].passed  # 0.925 >= 0.6
        assert any("s_tags=1.00" in r for r in r1[0].reasons)
        assert any("s_hist=0.50" in r for r in r1[0].reasons)

    def test_freshness_decay(self, vault):
        m = SkillMatcher(vault)
        stale = game(fetched=NOW - timedelta(seconds=90))  # 1.5x of 60s bound
        r = m.match(signal(), stale, now=NOW)
        expected = W_SIGNAL + W_TAGS + W_FRESH * 0.5 + W_HIST * 0.5
        assert r[0].score == pytest.approx(expected)
        dead = game(fetched=NOW - timedelta(seconds=120))  # 2x bound
        r2 = m.match(signal(), dead, now=NOW)
        assert r2[0].score == pytest.approx(W_SIGNAL + W_TAGS + W_HIST * 0.5)

    def test_history_factor(self, vault):
        vault.update_frontmatter("02-trading-skills/garbage-time-mispricing.md",
                                 {"win_rate": 0.8, "sample_size": 25}, caller="analyst")
        r = SkillMatcher(vault).match(signal(), game(), now=NOW)
        assert any("s_hist=1.00" in x for x in r[0].reasons)
        vault.update_frontmatter("02-trading-skills/garbage-time-mispricing.md",
                                 {"sample_size": 19}, caller="analyst")
        r2 = SkillMatcher(vault).match(signal(), game(), now=NOW)
        assert any("s_hist=0.50" in x for x in r2[0].reasons)  # low-sample prior

    def test_threshold_boundary_passes_at_equal(self, vault):
        vault.update_frontmatter("02-trading-skills/garbage-time-mispricing.md",
                                 {"confidence_threshold": 0.925}, caller="admin")
        r = SkillMatcher(vault).match(signal(), game(), now=NOW)
        assert r[0].passed  # score == threshold -> >= passes
