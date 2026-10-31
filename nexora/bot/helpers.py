"""Helpers shared by cogs."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

if TYPE_CHECKING:
    from nexora.bot import NexoraBot


def bot_of(interaction: discord.Interaction) -> "NexoraBot":
    return interaction.client  # type: ignore[return-value]


async def has_any_role(member: discord.Member, role_ids: list[int]) -> bool:
    ids = {int(r) for r in (role_ids or []) if str(r).isdigit()}
    return any(r.id in ids for r in member.roles)


def manager_check():
    """Manage Server, or one of the roles configured for dashboard access."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise app_commands.NoPrivateMessage()
        if interaction.user.guild_permissions.manage_guild:
            return True
        general = await bot_of(interaction).settings.get(interaction.guild.id, "general")
        if await has_any_role(interaction.user, general["dashboard_role_ids"]):
            return True
        raise app_commands.CheckFailure("You need **Manage Server** (or a Nexora manager role) to do that.")

    return app_commands.check(predicate)


async def log_event(
    bot: "NexoraBot",
    guild: discord.Guild,
    category: str,
    event_type: str,
    summary: str,
    embed: discord.Embed | None = None,
    *,
    user_id: int | None = None,
    moderator_id: int | None = None,
    channel_id: int | None = None,
    data: dict[str, Any] | None = None,
    files: list[discord.File] | None = None,
) -> None:
    cog = bot.get_cog("Logging")
    if cog is not None:
        await cog.record(  # type: ignore[attr-defined]
            guild,
            category,
            event_type,
            summary,
            embed,
            user_id=user_id,
            moderator_id=moderator_id,
            channel_id=channel_id,
            data=data,
            files=files,
        )


def can_act_on(actor: discord.Member, target: discord.Member) -> str | None:
    """Return an error message if `actor` may not moderate `target`, else None."""
    if target.id == actor.id:
        return "You can't do that to yourself."
    if target.id == actor.guild.owner_id:
        return "You can't moderate the server owner."
    if actor.id != actor.guild.owner_id and target.top_role >= actor.top_role:
        return "That member's highest role is equal to or above yours."
    me = actor.guild.me
    if me and target.top_role >= me.top_role:
        return "That member's highest role is equal to or above mine."
    return None
