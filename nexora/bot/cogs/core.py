"""Core commands: help, ping, info, dashboard link, config backup, housekeeping tasks."""
from __future__ import annotations

import io
import json
import time
from datetime import date

import discord
from discord import app_commands
from discord.ext import commands, tasks

from nexora.bot.helpers import manager_check
from nexora.core import schema
from nexora.core.permissions import settings_problems
from nexora.core.utils import BRAND, brand_embed, err, human_duration, ok

HELP_CATEGORIES = {
    "Moderation": "ban, unban, kick, timeout, untimeout, warn, warnings, clearwarnings, delwarn, purge, slowmode, lock, unlock, nickname, case, history, note, modstats, appeal, raid end",
    "Tickets": "ticket panel, ticket close, ticket claim, ticket transfer, ticket add, ticket remove, ticket priority, ticket rename, ticket reopen",
    "Applications": "apply, applications",
    "Giveaways": "giveaway start, giveaway end, giveaway reroll, giveaway cancel, giveaway extend, giveaway list",
    "Leveling & Economy": "rank, leaderboard, balance, daily, weekly, work, pay, shop, buy, inventory, achievements",
    "Community": "suggest, suggestion, poll, endpoll, birthday set, birthday remove, birthday list, remind, reminders",
    "Roles": "role add, role remove, role temp, role menu",
    "Utility": "help, ping, about, serverinfo, userinfo, avatar, dashboard, stats, ask, announce, embed",
    "Admin": "config export, config import, eco give, eco take, xp set, xp add, xp reset",
}


class Core(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.housekeeping.start()

    def cog_unload(self):
        self.housekeeping.cancel()

    @app_commands.command(name="help", description="Show Nexora's commands")
    async def help(self, interaction: discord.Interaction):
        embed = brand_embed("Nexora Commands", "All commands are slash commands. Configure everything on the dashboard.")
        for cat, cmds in HELP_CATEGORIES.items():
            embed.add_field(name=cat, value=" ".join(f"`/{c.strip()}`" for c in cmds.split(",")), inline=False)
        view = discord.ui.View()
        url = self.bot.config.public_url
        view.add_item(discord.ui.Button(label="Dashboard", url=f"{url}/dashboard"))
        view.add_item(discord.ui.Button(label="Commands", url=f"{url}/commands"))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(description="Check the bot's latency")
    async def ping(self, interaction: discord.Interaction):
        start = time.perf_counter()
        await self.bot.db.fetchval("SELECT 1")
        db_ms = (time.perf_counter() - start) * 1000
        await interaction.response.send_message(
            embed=brand_embed("🏓 Pong!", f"Gateway: **{self.bot.latency * 1000:.0f}ms**\nDatabase: **{db_ms:.1f}ms**"),
            ephemeral=True,
        )

    @app_commands.command(description="About Nexora")
    async def about(self, interaction: discord.Interaction):
        embed = brand_embed("Nexora", "The all-in-one Discord management platform.")
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)))
        embed.add_field(name="Uptime", value=human_duration(time.time() - self.bot.started_at))
        embed.add_field(name="Commands run", value=str(self.bot.command_count))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Open this server's Nexora dashboard")
    @app_commands.guild_only()
    async def dashboard(self, interaction: discord.Interaction):
        url = f"{self.bot.config.public_url}/dashboard/{interaction.guild_id}"
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open dashboard", url=url))
        await interaction.response.send_message(embed=brand_embed("Dashboard", f"Manage Nexora at {url}"), view=view, ephemeral=True)

    @app_commands.command(description="Information about this server")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        assert g
        embed = brand_embed(g.name, g.description)
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="Owner", value=f"<@{g.owner_id}>")
        embed.add_field(name="Members", value=str(g.member_count))
        embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
        embed.add_field(name="Channels", value=f"{len(g.text_channels)} text · {len(g.voice_channels)} voice")
        embed.add_field(name="Roles", value=str(len(g.roles)))
        embed.add_field(name="Boosts", value=f"{g.premium_subscription_count} (tier {g.premium_tier})")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Information about a member")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        m = member or interaction.user
        assert isinstance(m, discord.Member)
        embed = brand_embed(str(m), color=m.color.value or BRAND)
        embed.set_thumbnail(url=m.display_avatar.url)
        embed.add_field(name="ID", value=str(m.id))
        embed.add_field(name="Account created", value=discord.utils.format_dt(m.created_at, "R"))
        if m.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(m.joined_at, "R"))
        roles = [r.mention for r in reversed(m.roles) if not r.is_default()]
        embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles[:20]) or "None", inline=False)
        warns = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=? AND active=1", (interaction.guild_id, m.id), 0
        )
        embed.add_field(name="Active warnings", value=str(warns))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Show a user's avatar")
    async def avatar(self, interaction: discord.Interaction, user: discord.User | None = None):
        u = user or interaction.user
        embed = brand_embed(f"{u.name}'s avatar")
        embed.set_image(url=u.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    config_group = app_commands.Group(name="config", description="Back up or restore this server's Nexora configuration", guild_only=True)

    @config_group.command(name="export", description="Download this server's Nexora configuration as JSON")
    @manager_check()
    async def config_export(self, interaction: discord.Interaction):
        data = await self.bot.settings.export_guild(interaction.guild_id)
        payload = {"nexora_config_version": 1, "guild_id": interaction.guild_id, "modules": data}
        file = discord.File(io.BytesIO(json.dumps(payload, indent=2).encode()), filename=f"nexora-config-{interaction.guild_id}.json")
        await interaction.response.send_message("Here is your configuration backup.", file=file, ephemeral=True)

    @config_group.command(name="import", description="Restore a Nexora configuration backup (JSON file)")
    @manager_check()
    async def config_import(self, interaction: discord.Interaction, file: discord.Attachment):
        if file.size > 512_000:
            return await interaction.response.send_message(embed=err("File too large."), ephemeral=True)
        try:
            payload = json.loads(await file.read())
            modules = payload["modules"]
            if not isinstance(modules, dict):
                raise ValueError("this isn't a Nexora configuration backup")
            # Same rule as the dashboard: a backup can't set up role rewards/autoroles or dashboard access
            # that the person importing it couldn't set up by hand.
            for module, values in modules.items():
                if module in schema.MODULES and isinstance(values, dict):
                    clean = schema.validate(module, values, partial=True)
                    current = await self.bot.settings.get(interaction.guild_id, module)
                    problems = settings_problems(interaction.user, interaction.guild, module, clean, current)  # type: ignore[arg-type]
                    if problems:
                        key, why = next(iter(problems.items()))
                        raise ValueError(f"{module}.{key}: {why}")
            imported = await self.bot.settings.import_guild(interaction.guild_id, modules)
        except Exception as exc:  # noqa: BLE001 - user-supplied file, report anything
            return await interaction.response.send_message(embed=err(f"Could not import: {exc}"), ephemeral=True)
        await interaction.response.send_message(embed=ok(f"Imported {len(imported)} modules: {', '.join(imported)}"), ephemeral=True)

    @app_commands.command(description="Quick server statistics for the last 7 days")
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM stats_daily WHERE guild_id=? ORDER BY day DESC LIMIT 7", (interaction.guild_id,)
        )
        joins = sum(r["joins"] for r in rows)
        leaves = sum(r["leaves"] for r in rows)
        msgs = sum(r["messages"] for r in rows)
        embed = brand_embed("Last 7 days")
        embed.add_field(name="Joins", value=str(joins))
        embed.add_field(name="Leaves", value=str(leaves))
        embed.add_field(name="Net growth", value=f"{joins - leaves:+d}")
        embed.add_field(name="Messages", value=str(msgs))
        embed.add_field(name="Voice minutes", value=str(sum(r["voice_minutes"] for r in rows)))
        embed.add_field(name="Mod actions", value=str(sum(r["mod_actions"] for r in rows)))
        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------- housekeeping
    @tasks.loop(hours=1)
    async def housekeeping(self):
        db = self.bot.db
        today = date.today().isoformat()
        for g in self.bot.guilds:
            await db.execute(
                """INSERT INTO stats_daily (guild_id, day, member_count) VALUES (?, ?, ?)
                   ON CONFLICT(guild_id, day) DO UPDATE SET member_count = excluded.member_count""",
                (g.id, today, g.member_count),
            )
            await self.bot.upsert_guild(g)
            s = await self.bot.settings.get(g.id, "logging")
            await db.execute(
                "DELETE FROM log_events WHERE guild_id = ? AND created_at < datetime('now', ?)",
                (g.id, f"-{int(s['retention_days'])} days"),
            )
        await db.execute("DELETE FROM web_sessions WHERE expires_at < ?", (time.time(),))
        await db.execute("DELETE FROM error_log WHERE created_at < datetime('now', '-30 days')")
        await db.execute("DELETE FROM automation_runs WHERE created_at < datetime('now', '-30 days')")

    @housekeeping.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Core(bot))
