"""Nexora Discord bot."""
from __future__ import annotations

import logging
import time
import traceback
from datetime import date

import discord
from discord import app_commands
from discord.ext import commands

from nexora.config import Config
from nexora.core.settings import SettingsService
from nexora.core.utils import err
from nexora.db import Database

log = logging.getLogger("nexora.bot")

EXTENSIONS = [
    "nexora.bot.cogs.core",
    "nexora.bot.cogs.logging",
    "nexora.bot.cogs.moderation",
    "nexora.bot.cogs.automod",
    "nexora.bot.cogs.tickets",
    "nexora.bot.cogs.applications",
    "nexora.bot.cogs.giveaways",
    "nexora.bot.cogs.welcome",
    "nexora.bot.cogs.roles",
    "nexora.bot.cogs.leveling",
    "nexora.bot.cogs.economy",
    "nexora.bot.cogs.community",
    "nexora.bot.cogs.utility",
    "nexora.bot.cogs.automation",
    "nexora.bot.cogs.ai",
]


class NexoraBot(commands.Bot):
    def __init__(self, config: Config, db: Database):
        intents = discord.Intents.default()
        intents.members = True          # privileged: joins/leaves, member logs, welcome
        intents.message_content = True  # privileged: automod, logs, leveling, custom commands
        intents.presences = False
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            max_messages=5000,  # message cache used for before/after edit & delete logs
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True, replied_user=False),
            owner_ids=set(config.owner_ids) or None,
        )
        self.config = config
        self.db = db
        self.settings = SettingsService(db)
        self.started_at = time.time()
        self.command_count = 0
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", self.config.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands (may take a few minutes to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d servers", self.user, self.user.id if self.user else "?", len(self.guilds))
        for g in self.guilds:
            await self.upsert_guild(g)

    async def upsert_guild(self, guild: discord.Guild) -> None:
        await self.db.execute(
            """INSERT INTO guilds (guild_id, name, icon, owner_id, member_count, active) VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(guild_id) DO UPDATE SET name=excluded.name, icon=excluded.icon, owner_id=excluded.owner_id,
               member_count=excluded.member_count, active=1""",
            (guild.id, guild.name, guild.icon.key if guild.icon else None, guild.owner_id, guild.member_count or 0),
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.upsert_guild(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.db.execute("UPDATE guilds SET active = 0 WHERE guild_id = ?", (guild.id,))
        self.settings.invalidate(guild.id)

    # ---------------------------------------------------------------- stats
    async def bump_stat(self, guild_id: int, column: str, amount: int = 1) -> None:
        assert column in {"joins", "leaves", "messages", "voice_minutes", "mod_actions"}
        await self.db.execute(
            f"""INSERT INTO stats_daily (guild_id, day, {column}) VALUES (?, ?, ?)
                ON CONFLICT(guild_id, day) DO UPDATE SET {column} = {column} + excluded.{column}""",
            (guild_id, date.today().isoformat(), amount),
        )

    async def track_staff(self, guild_id: int, user_id: int | None) -> None:
        if not user_id:
            return
        await self.db.execute(
            """INSERT INTO staff_activity (guild_id, user_id, day, actions) VALUES (?, ?, ?, 1)
               ON CONFLICT(guild_id, user_id, day) DO UPDATE SET actions = actions + 1""",
            (guild_id, user_id, date.today().isoformat()),
        )

    async def track_feature(self, guild_id: int | None, feature: str) -> None:
        if not guild_id:
            return
        await self.db.execute(
            """INSERT INTO feature_usage (guild_id, feature, count) VALUES (?, ?, 1)
               ON CONFLICT(guild_id, feature) DO UPDATE SET count = count + 1""",
            (guild_id, feature),
        )

    def trigger(self, guild: discord.Guild, name: str, **context) -> None:
        """Fire an automation trigger (handled by the automation cog)."""
        self.dispatch("nexora_trigger", guild, name, context)

    async def on_app_command_completion(self, interaction: discord.Interaction, command) -> None:
        self.command_count += 1
        await self.track_feature(interaction.guild_id, f"cmd:{command.qualified_name}")

    # --------------------------------------------------------------- errors
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need these permissions: " + ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = "I'm missing permissions: " + ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Slow down! Try again in {error.retry_after:.0f}s."
        elif isinstance(error, (app_commands.CheckFailure, app_commands.NoPrivateMessage)):
            msg = str(error) or "You can't use this command here."
        elif isinstance(original, discord.Forbidden):
            msg = "Discord refused that action. Check that my role is above the target and I have the right permissions."
        else:
            msg = "Something went wrong running that command. The error was recorded for the server owner."
            log.error("Command error", exc_info=original)
            await self.db.log_error(
                f"command:{interaction.command.qualified_name if interaction.command else '?'}",
                repr(original),
                "".join(traceback.format_exception(original)),
                interaction.guild_id,
            )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=err(msg), ephemeral=True)
            else:
                await interaction.response.send_message(embed=err(msg), ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        import sys

        exc = sys.exc_info()[1]
        log.exception("Error in event %s", event_method)
        guild_id = None
        for a in args:
            g = getattr(a, "guild", None)
            if g is not None:
                guild_id = g.id
                break
        await self.db.log_error(f"event:{event_method}", repr(exc), traceback.format_exc(), guild_id)
