import pytest

pytest.importorskip("discord")

import discord  # noqa: E402

from kalshi_bots.discord_gateway import (  # noqa: E402
    _is_authorized, _parse_custom_id, build_card_embed,
)


class FakeRole:
    def __init__(self, id_):
        self.id = id_


class FakeUser:
    def __init__(self, roles=None):
        self.roles = roles or []


class TestAuthorization:
    def test_no_role_configured_fails_open(self, monkeypatch):
        monkeypatch.delenv("APPROVER_ROLE_ID", raising=False)
        assert _is_authorized(FakeUser(roles=[])) is True

    def test_role_configured_and_present(self, monkeypatch):
        monkeypatch.setenv("APPROVER_ROLE_ID", "555")
        assert _is_authorized(FakeUser(roles=[FakeRole(555)])) is True

    def test_role_configured_and_absent(self, monkeypatch):
        monkeypatch.setenv("APPROVER_ROLE_ID", "555")
        assert _is_authorized(FakeUser(roles=[FakeRole(1), FakeRole(2)])) is False

    def test_user_with_no_roles_attribute(self, monkeypatch):
        monkeypatch.setenv("APPROVER_ROLE_ID", "555")
        assert _is_authorized(object()) is False


class TestCustomIdParsing:
    def test_approve(self):
        assert _parse_custom_id("approve:kb-abc123") == ("approved", "kb-abc123")

    def test_reject(self):
        assert _parse_custom_id("reject:kb-abc123") == ("rejected", "kb-abc123")

    def test_coid_containing_colons(self):
        assert _parse_custom_id("approve:kb-abc:extra") == ("approved", "kb-abc:extra")

    def test_unrecognized(self):
        assert _parse_custom_id("something_else") is None
        assert _parse_custom_id("") is None


class TestCardEmbed:
    def test_embed_and_buttons(self):
        message = {"kind": "trade_card", "client_order_id": "kb-xyz",
                  "text": "TRADE CARD [overreaction] BUY yes 10x T @ 60c"}
        embed, view = build_card_embed(message)
        assert isinstance(embed, discord.Embed)
        assert embed.title == "Trade Proposal"
        assert "T @ 60c" in embed.description
        buttons = list(view.children)
        assert len(buttons) == 2
        approve = next(b for b in buttons if b.label == "Approve")
        reject = next(b for b in buttons if b.label == "Reject")
        assert approve.custom_id == "approve:kb-xyz"
        assert reject.custom_id == "reject:kb-xyz"
        assert approve.style == discord.ButtonStyle.success
        assert reject.style == discord.ButtonStyle.danger
