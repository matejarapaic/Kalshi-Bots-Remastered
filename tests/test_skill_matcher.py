"""skill-matcher tests. Spec: skills/skill-matcher/SKILL.md."""
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bots.skills.skill_matcher import SkillMatcher, derive_condition_tags
from kalshi_bots.skills.vault import Vault
from kalshi_bots.types import CryptoSignal, WindowRef

NOW = datetime(2026, 7, 23, 1, 20, tzinfo=timezone.utc)


def skill_fm(**over):
    fm = {
        "skill": "btc-15min-fair-value", "families": ["KXBTC15M"],
        "signal_types": ["fair-value-candidate"],
        "market_conditions": ["live", "midpoint", "opening"],
        "confidence_threshold": 0.6, "risk_profile": "medium",
        "win_rate": None, "sample_size": 0,
        "status": "confirmed", "last_updated": "2026-07-22",
    }
    fm.update(over)
    return fm


def signal(sig_type="fair-value-candidate", phase="midpoint", payload=None,
           emitted_at=NOW, series="KXBTC15M"):
    w = WindowRef(series_ticker=series, event_ticker="KXBTC15M-26JUL222130",
                  market_ticker="KXBTC15M-26JUL222130-30",
                  opens_at=NOW, closes_at=NOW + timedelta(minutes=10),
                  strike=66010.86)
    return CryptoSignal(signal_type=sig_type, series_ticker=series,
                        market_ticker=w.market_ticker, window=w, phase=phase,
                        payload=payload or {}, emitted_at=emitted_at)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "02-trading-skills").mkdir(parents=True)
    return Vault(root=str(root))


def add_skill(vault, name="btc-15min-fair-value", **over):
    vault.write_note(f"02-trading-skills/{name}.md",
                     skill_fm(skill=name, **over), "# skill", caller="admin")


class TestConditionTags:
    def test_always_live_plus_phase(self):
        assert derive_condition_tags(signal(phase="opening")) == ["live", "opening"]

    def test_high_vol_tag_from_sigma(self):
        tags = derive_condition_tags(signal(payload={"sigma": 1.4}))
        assert "high-volatility" in tags
        assert "high-volatility" not in derive_condition_tags(
            signal(payload={"sigma": 0.4}))

    def test_thin_book_tag(self):
        assert "thin-book" in derive_condition_tags(
            signal(payload={"thin_book": True}))


class TestGates:
    def test_confirmed_skill_matches(self, vault):
        add_skill(vault)
        matches = SkillMatcher(vault).match(signal(), now=NOW)
        assert len(matches) == 1 and matches[0].skill_name == "btc-15min-fair-value"
        assert matches[0].passed

    def test_draft_skill_structurally_invisible(self, vault):
        add_skill(vault, status="draft")
        assert SkillMatcher(vault).match(signal(), now=NOW) == []

    def test_signal_type_gate(self, vault):
        add_skill(vault, signal_types=["some-other-candidate"])
        assert SkillMatcher(vault).match(signal(), now=NOW) == []

    def test_family_gate(self, vault):
        add_skill(vault, families=["KXETH15M"])
        assert SkillMatcher(vault).match(signal(), now=NOW) == []

    def test_family_all_wildcard(self, vault):
        add_skill(vault, families=["all"])
        assert len(SkillMatcher(vault).match(signal(), now=NOW)) == 1

    def test_phase_tag_gate(self, vault):
        add_skill(vault, market_conditions=["near_close"])  # never entered here
        assert SkillMatcher(vault).match(signal(phase="midpoint"), now=NOW) == []

    def test_lifecycle_signals_never_match(self, vault):
        add_skill(vault)
        for t in ("window-open", "phase-change", "window-close"):
            assert SkillMatcher(vault).match(signal(sig_type=t), now=NOW) == []

    def test_invalid_confidence_threshold_excluded(self, vault, tmp_path):
        # the vault's write-time schema check catches this on writes; the
        # matcher's own guard covers notes hand-edited on disk — bypass the
        # vault writer to simulate that
        import yaml
        fm = skill_fm(confidence_threshold=1.7)
        raw = "---\n" + yaml.safe_dump(fm) + "---\n\n# skill\n"
        (tmp_path / "vault" / "02-trading-skills" / "bad.md").write_text(raw)
        assert SkillMatcher(vault).match(signal(), now=NOW) == []


class TestScoring:
    def test_freshness_decay(self, vault):
        add_skill(vault)
        m = SkillMatcher(vault)
        fresh = m.match(signal(emitted_at=NOW), now=NOW)[0]
        # default bound 10s: 15s-old signal decays s_fresh to 0.5
        stale = m.match(signal(emitted_at=NOW - timedelta(seconds=15)), now=NOW)[0]
        dead = m.match(signal(emitted_at=NOW - timedelta(seconds=30)), now=NOW)[0]
        assert fresh.score > stale.score > dead.score

    def test_reasons_are_auditable(self, vault):
        add_skill(vault)
        m = SkillMatcher(vault).match(signal(), now=NOW)[0]
        assert any("gate:signal_type" in r for r in m.reasons)
        assert any("gate:family" in r for r in m.reasons)
        assert any("s_fresh" in r for r in m.reasons)

    def test_history_component_uses_demo_stats(self, vault):
        add_skill(vault, name="seasoned", demo_win_rate=0.8, demo_sample_size=50)
        add_skill(vault, name="rookie")
        out = SkillMatcher(vault).match(signal(), now=NOW)
        by_name = {m.skill_name: m for m in out}
        assert by_name["seasoned"].score > by_name["rookie"].score
