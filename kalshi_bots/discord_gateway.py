"""Discord gateway transport: real slash-command and button interactivity.

DiscordTransport (skills/discord_bot.py) is REST-only — it can send messages
but cannot receive anything, since Discord only delivers slash-command
invocations and button clicks over a persistent gateway (websocket)
connection or a public HTTPS interactions endpoint. A public endpoint isn't
practical for a system running locally, so this uses discord.py's gateway
client instead, in its own asyncio event loop on a background thread —
the same "background thread running a long-lived loop" pattern already used
for the orchestrator's poll loop in dashboard.main().

GatewayTransport.send() bridges from the synchronous calling thread via
asyncio.run_coroutine_threadsafe, so it implements the exact same
`send(message: dict) -> str` interface as ConsoleTransport/DiscordTransport —
DiscordBot itself needs no changes to use it.

Button clicks are handled by parsing the raw custom_id ("approve:<coid>" /
"reject:<coid>") in a generic on_interaction handler, not a per-message
discord.ui.View callback bound to a specific in-memory View instance — a
trade card created before a process restart must still be clickable, and
there is no view object left in memory to match a click back to.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

import discord
from discord import app_commands

from kalshi_bots.skills.discord_bot import DiscordUnavailable

log = logging.getLogger(__name__)

DISCORD_MAX_LEN = 2000
READY_TIMEOUT_S = 25  # observed live: a fresh connect can take >15s under reconnect churn


def _is_authorized(user) -> bool:
    """No APPROVER_ROLE_ID configured -> fail OPEN (allow). The discord-bot
    spec requires this role for manual_approve mode at construction time;
    here, in autonomous-mode /halt or an unconfigured deployment, refusing
    to let anyone halt trading because a role id was never set is the worse
    failure mode than being permissive."""
    role_id = os.environ.get("APPROVER_ROLE_ID")
    if not role_id:
        return True
    roles = getattr(user, "roles", None) or []
    return any(str(r.id) == role_id for r in roles)


def _parse_custom_id(custom_id: str) -> tuple[str, str] | None:
    """'approve:kb-abc123' -> ('approved', 'kb-abc123'); None if unrecognized."""
    if custom_id.startswith("approve:"):
        return "approved", custom_id[len("approve:"):]
    if custom_id.startswith("reject:"):
        return "rejected", custom_id[len("reject:"):]
    return None


def build_card_embed(message: dict) -> tuple[discord.Embed, discord.ui.View]:
    """message is the payload DiscordBot.send_trade_card hands to transport.send():
    {"kind": "trade_card", "client_order_id": ..., "text": <rendered card>, ...}."""
    coid = message["client_order_id"]
    embed = discord.Embed(title="Trade Proposal", description=message.get("text", ""),
                         color=discord.Color.blurple())
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.success,
                                    label="Approve", custom_id=f"approve:{coid}"))
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.danger,
                                    label="Reject", custom_id=f"reject:{coid}"))
    return embed, view


def _register_commands(tree: app_commands.CommandTree, client: "_GatewayClient") -> None:
    """Registered once at tree-creation time (pure in-memory); syncing them
    to Discord happens separately in setup_hook, once the client can make
    API calls."""

    def _handle(cmd: str, arg: str, user: str) -> str:
        bot = client.bot_ref
        return bot.handle_command(cmd, arg, user=user) if bot else "bot not ready yet"

    @tree.command(name="positions", description="Show open positions and exposure")
    async def positions_cmd(interaction: discord.Interaction):
        text = _handle("positions", "", str(interaction.user))
        await interaction.response.send_message((text or "(no open positions)")[:DISCORD_MAX_LEN])

    @tree.command(name="pnl", description="Show daily realized profit and loss")
    async def pnl_cmd(interaction: discord.Interaction):
        await interaction.response.send_message(_handle("pnl", "", str(interaction.user))[:DISCORD_MAX_LEN])

    @tree.command(name="skills",
                 description="List confirmed trading skills with win rate and sample size")
    async def skills_cmd(interaction: discord.Interaction):
        text = _handle("skills", "", str(interaction.user))
        await interaction.response.send_message((text or "(no confirmed skills)")[:DISCORD_MAX_LEN])

    @tree.command(name="window", description="Show the active 15-minute window's state")
    async def window_cmd(interaction: discord.Interaction):
        await interaction.response.send_message(_handle("window", "", str(interaction.user))[:DISCORD_MAX_LEN])

    @tree.command(name="halt", description="Stop all new entries immediately (open positions can still exit)")
    @app_commands.describe(reason="Why you're halting (optional)")
    async def halt_cmd(interaction: discord.Interaction, reason: str = ""):
        if not _is_authorized(interaction.user):
            await interaction.response.send_message(
                "You are not authorized to halt trading.", ephemeral=True)
            return
        await interaction.response.send_message(_handle("halt", reason, str(interaction.user)))

    @tree.command(name="resume", description="Clear an active halt")
    async def resume_cmd(interaction: discord.Interaction):
        if not _is_authorized(interaction.user):
            await interaction.response.send_message(
                "You are not authorized to resume trading.", ephemeral=True)
            return
        await interaction.response.send_message(_handle("resume", "", str(interaction.user)))


class _GatewayClient(discord.Client):
    def __init__(self, *, intents: discord.Intents, guild_id: str | None):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.bot_ref = None  # set via GatewayTransport.bind()
        self._guild_id_override = guild_id
        self._synced = False
        self.ready_event = threading.Event()
        _register_commands(self.tree, self)

    async def on_ready(self):
        if not self._synced:
            guild = None
            if self._guild_id_override:
                guild = self.get_guild(int(self._guild_id_override))
            elif len(self.guilds) == 1:
                guild = self.guilds[0]
            if guild:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("slash commands synced to guild %s (%s) — instant", guild.id, guild.name)
            else:
                await self.tree.sync()  # global fallback: ~1hr propagation
                log.warning("bot is in %d guild(s) with no DISCORD_GUILD_ID set — "
                           "synced globally instead (can take up to an hour)",
                           len(self.guilds))
            self._synced = True
        self.ready_event.set()

    async def on_interaction(self, interaction: discord.Interaction):
        # Slash commands are dispatched by self.tree itself; only handle the
        # raw component (button) clicks here.
        if interaction.type != discord.InteractionType.component:
            return
        parsed = _parse_custom_id(interaction.data.get("custom_id", ""))
        if parsed is None:
            return
        decision, coid = parsed
        authorized = _is_authorized(interaction.user)
        ok = (self.bot_ref.resolve_card(coid, decision, str(interaction.user), authorized)
              if self.bot_ref else False)
        if not authorized:
            await interaction.response.send_message(
                "You are not authorized to approve/reject trades.", ephemeral=True)
            return
        if ok:
            embed = (interaction.message.embeds[0] if interaction.message.embeds
                     else discord.Embed())
            embed.color = (discord.Color.green() if decision == "approved"
                           else discord.Color.red())
            embed.set_footer(text=f"{'Approved' if decision == 'approved' else 'Rejected'} "
                                  f"by {interaction.user}")
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.send_message(
                "This card already expired or was already decided.", ephemeral=True)


class GatewayTransport:
    """Drop-in replacement for ConsoleTransport/DiscordTransport that also
    receives slash commands and button clicks. Call bind() then start()
    before use."""

    def __init__(self, token: str, channel_id: str, guild_id: str | None = None):
        self.token = token
        self.channel_id = int(channel_id)
        self._client = _GatewayClient(intents=discord.Intents.default(), guild_id=guild_id)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def bind(self, discord_bot) -> None:
        """Give the gateway a reference to the owning DiscordBot so slash
        commands and button clicks can call handle_command/resolve_card."""
        self._client.bot_ref = discord_bot

    def start(self, timeout_s: float = READY_TIMEOUT_S) -> None:
        self._loop = asyncio.new_event_loop()

        def runner():
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._client.start(self.token))
            except Exception:
                log.exception("discord gateway client crashed")

        self._thread = threading.Thread(target=runner, daemon=True, name="discord-gateway")
        self._thread.start()
        if not self._client.ready_event.wait(timeout=timeout_s):
            raise DiscordUnavailable("gateway did not become ready in time")

    def stop(self) -> None:
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)

    def send(self, message: dict) -> str:
        if self._loop is None:
            raise DiscordUnavailable("gateway not started")
        future = asyncio.run_coroutine_threadsafe(self._async_send(message), self._loop)
        try:
            return future.result(timeout=15)
        except Exception as e:
            raise DiscordUnavailable(str(e)) from e

    async def _async_send(self, message: dict) -> str:
        channel = self._client.get_channel(self.channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(self.channel_id)
        if message.get("kind") == "trade_card":
            embed, view = build_card_embed(message)
            sent = await channel.send(embed=embed, view=view)
        else:
            sent = await channel.send(content=message.get("text", "")[:DISCORD_MAX_LEN])
        return str(sent.id)
