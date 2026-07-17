import pytest

from kalshi_bots.skills.vault import (
    Vault, VaultNotFound, VaultSchemaError, VaultScopeError,
)
from kalshi_bots.types import VaultQuery

SKILL_NOTE = """---
skill: test-skill
sports: [mlb]
market_conditions: [live, endgame]
confidence_threshold: 0.6
risk_profile: low
win_rate: null
sample_size: 0
status: confirmed
last_updated: 2026-07-17
---

# Test Skill

Body text with trailing spaces
and unicode — ✓.
"""


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    for d in ("00-meta", "02-trading-skills", "03-market-context/active-games",
              "04-trade-history/trades"):
        (root / d).mkdir(parents=True)
    (root / "02-trading-skills" / "test-skill.md").write_text(SKILL_NOTE)
    return Vault(root=str(root))


class TestRoundTrip:
    def test_byte_fidelity(self, vault, tmp_path):
        note = vault.read_note("02-trading-skills/test-skill.md")
        original = (tmp_path / "vault" / "02-trading-skills" / "test-skill.md").read_text()
        assert note.body in original
        assert note.body.endswith("unicode — ✓.\n")
        # write back read content; body must be identical after re-read
        vault.write_note("02-trading-skills/test-skill.md", note.frontmatter,
                         note.body, caller="admin")
        vault.invalidate()
        assert vault.read_note("02-trading-skills/test-skill.md").body == note.body

    def test_no_frontmatter_file(self, vault, tmp_path):
        (tmp_path / "vault" / "00-meta" / "plain.md").write_text("just text\n")
        note = vault.read_note("00-meta/plain.md")
        assert note.frontmatter == {} and note.body == "just text\n"


class TestQuery:
    def test_status_filter(self, vault, tmp_path):
        draft = SKILL_NOTE.replace("status: confirmed", "status: draft")
        (tmp_path / "vault" / "02-trading-skills" / "draft.md").write_text(draft)
        got = vault.query(VaultQuery(directory="02-trading-skills",
                                     frontmatter_filters={"status": "confirmed"}))
        assert [n.path for n in got] == ["02-trading-skills/test-skill.md"]

    def test_tag_membership(self, vault):
        assert vault.query(VaultQuery(directory="02-trading-skills",
                                      tag_filters=["live"]))
        assert not vault.query(VaultQuery(directory="02-trading-skills",
                                          tag_filters=["pregame"]))

    def test_malformed_note_skipped(self, vault, tmp_path):
        (tmp_path / "vault" / "02-trading-skills" / "bad.md").write_text(
            "---\n: : broken yaml [\n---\nbody")
        got = vault.query(VaultQuery(directory="02-trading-skills"))
        assert [n.path for n in got] == ["02-trading-skills/test-skill.md"]

    def test_empty_dir(self, vault):
        assert vault.query(VaultQuery(directory="04-trade-history/trades")) == []


class TestScopes:
    def test_analyst_stats_allowed(self, vault):
        vault.update_frontmatter("02-trading-skills/test-skill.md",
                                 {"win_rate": 0.7, "sample_size": 10}, caller="analyst")
        assert vault.read_note("02-trading-skills/test-skill.md").frontmatter["sample_size"] == 10

    def test_analyst_threshold_denied(self, vault):
        with pytest.raises(VaultScopeError):
            vault.update_frontmatter("02-trading-skills/test-skill.md",
                                     {"confidence_threshold": 0.1}, caller="analyst")

    def test_trader_skill_dir_denied(self, vault):
        with pytest.raises(VaultScopeError):
            vault.write_note("02-trading-skills/evil.md", {}, "x", caller="trader")

    def test_trader_trade_notes_allowed(self, vault):
        vault.write_note("04-trade-history/trades/t1.md", {"espn_event_id": "1"},
                         "trade", caller="trader")


class TestSchema:
    def test_missing_field_rejected(self, vault):
        with pytest.raises(VaultSchemaError):
            vault.write_note("02-trading-skills/new.md",
                             {"skill": "x", "status": "draft"}, "b", caller="admin")

    def test_bad_status_rejected(self, vault):
        import yaml
        fm = yaml.safe_load(SKILL_NOTE.split("---")[1])
        fm["status"] = "bogus"
        with pytest.raises(VaultSchemaError):
            vault.write_note("02-trading-skills/new.md", fm, "b", caller="admin")

    def test_silent_key_drop_rejected(self, vault):
        with pytest.raises(VaultSchemaError):
            vault.write_note("02-trading-skills/test-skill.md",
                             {"skill": "test-skill"}, "b", caller="admin")


class TestCache:
    def test_expired_unchanged_no_reparse(self, vault):
        vault.read_note("02-trading-skills/test-skill.md")
        n = vault.parse_count
        # force expiry
        path = "02-trading-skills/test-skill.md"
        note, mtime, _ = vault._cache[path]
        vault._cache[path] = (note, mtime, 0.0)
        vault.read_note(path)
        assert vault.parse_count == n  # TTL refreshed without re-parse

    def test_external_modification_detected(self, vault, tmp_path):
        path = "02-trading-skills/test-skill.md"
        vault.read_note(path)
        f = tmp_path / "vault" / path
        import os
        f.write_text(SKILL_NOTE.replace("confirmed", "retired"))
        os.utime(f, (0, 12345))  # distinct mtime
        note, _, _ = vault._cache[path]
        vault._cache[path] = (note, 1.0, 0.0)  # expired + stale mtime
        assert vault.read_note(path).frontmatter["status"] == "retired"

    def test_deleted_note(self, vault, tmp_path):
        path = "02-trading-skills/test-skill.md"
        vault.read_note(path)
        (tmp_path / "vault" / path).unlink()
        vault._cache[path] = (vault._cache[path][0], vault._cache[path][1], 0.0)
        with pytest.raises(VaultNotFound):
            vault.read_note(path)


class TestAppend:
    def test_append_section(self, vault):
        vault.write_note("03-market-context/active-games/mlb-1.md",
                         {"espn_event_id": "1"}, "# Game\n", caller="game-monitor")
        vault.append_section("03-market-context/active-games/mlb-1.md",
                             "Signals", "- overreaction-candidate", caller="game-monitor")
        note = vault.read_note("03-market-context/active-games/mlb-1.md")
        assert "## Signals" in note.body and "overreaction-candidate" in note.body
        assert note.frontmatter == {"espn_event_id": "1"}
