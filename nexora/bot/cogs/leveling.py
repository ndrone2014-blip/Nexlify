"""Leveling: XP per message with cooldowns, multipliers, level-up messages, role rewards, leaderboards."""
from __future__ import annotations

import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from nexora.bot.helpers import has_any_role, manager_check
from nexora.core.leveling_math import level_from_xp, progress, progress_bar, total_xp_for_level
from nexora.core.utils import brand_embed, err, ok, render


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def add_xp(self, member: discord.Member, amount: int, channel: discord.abc.Messageable | None) -> int | None:
        """Add XP; returns the new level if the member levelled up."""
        g = member.guild
        row = await self.bot.db.fetchone("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?", (g.id, member.id))
        old_level = row["level"] if row else 0
        new_xp = max(0, (row["xp"] if row else 0) + amount)
        new_level = level_from_xp(new_xp)
        await self.bot.db.execute(
            """INSERT INTO levels (guild_id, user_id, xp, level, messages, last_xp_at) VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=excluded.xp, level=excluded.level, messages=messages+1, last_xp_at=excluded.last_xp_at""",
            (g.id, member.id, new_xp, new_level, time.time()))
        if new_level > old_level:
            await self.on_level_up(member, new_level, new_xp, channel)
            return new_level
        return None

    async def sync_rewards(self, member: discord.Member, level: int, s: dict) -> list[discord.Role]:
        rewards = sorted((r for r in s["rewards"] if r.get("role_id")), key=lambda r: r["level"])
        earned = [r for r in rewards if r["level"] <= level]
        if not earned:
            return []
        g = member.guild
        if s["stack_rewards"]:
            want = {r["role_id"] for r in earned}
            remove_ids: set[int] = set()
        else:
            want = {earned[-1]["role_id"]}
            remove_ids = {r["role_id"] for r in rewards} - want
        add = [r for r in (g.get_role(i) for i in want) if r and r not in member.roles and r < g.me.top_role]
        remove = [r for r in member.roles if r.id in remove_ids and r < g.me.top_role]
        try:
            if add:
                await member.add_roles(*add, reason=f"Level {level} reward")
            if remove:
                await member.remove_roles(*remove, reason="Level reward replaced")
        except discord.HTTPException:
            return []
        return add

    async def on_level_up(self, member: discord.Member, level: int, xp: int, channel):
        s = await self.bot.settings.get(member.guild.id, "leveling")
        given = await self.sync_rewards(member, level, s)
        if s["announce"]:
            target = member.guild.get_channel(s["announce_channel_id"]) if s["announce_channel_id"] else channel
            if target is not None:
                text = render(s["levelup_message"], member, member.guild, level=level, xp=xp)
                if given:
                    text += "\nRewards: " + " ".join(r.mention for r in given)
                try:
                    await target.send(text, allowed_mentions=discord.AllowedMentions(users=True))
                except discord.HTTPException:
                    pass
        econ = self.bot.get_cog("Economy")
        if econ:
            for milestone in (5, 10, 25, 50, 100):
                if level == milestone:
                    await econ.grant_achievement(member, f"level_{milestone}")
        self.bot.trigger(member.guild, "level_up", member=member, level=level)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        s = await self.bot.settings.get(message.guild.id, "leveling")
        if not s["enabled"] or message.channel.id in s["no_xp_channel_ids"] or await has_any_role(message.author, s["no_xp_role_ids"]):
            return
        last = await self.bot.db.fetchval("SELECT last_xp_at FROM levels WHERE guild_id=? AND user_id=?", (message.guild.id, message.author.id), 0)
        if time.time() - last < s["cooldown_seconds"]:
            return
        lo, hi = sorted((s["xp_min"], s["xp_max"]))
        amount = random.randint(lo, hi)
        mult = max([m["multiplier"] for m in s["multipliers"] if m.get("role_id") and message.author.get_role(m["role_id"])] or [1.0])
        await self.add_xp(message.author, int(amount * mult), message.channel)
        econ_s = await self.bot.settings.get(message.guild.id, "economy")
        if econ_s["enabled"] and econ_s["message_reward"]:
            econ = self.bot.get_cog("Economy")
            if econ:
                await econ.add_balance(message.guild.id, message.author.id, econ_s["message_reward"])

    # ------------------------------------------------------------ commands
    @app_commands.command(description="Show your (or someone's) level")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        m = member or interaction.user
        s = await self.bot.settings.get(interaction.guild_id, "leveling")
        if not s["enabled"]:
            return await interaction.response.send_message(embed=err("Leveling is disabled on this server."), ephemeral=True)
        row = await self.bot.db.fetchone("SELECT xp, messages FROM levels WHERE guild_id=? AND user_id=?", (interaction.guild_id, m.id))
        xp = row["xp"] if row else 0
        position = await self.bot.db.fetchval("SELECT COUNT(*) FROM levels WHERE guild_id=? AND xp > ?", (interaction.guild_id, xp), 0) + 1
        level, into, need = progress(xp)
        embed = brand_embed(f"{m.display_name}'s rank")
        embed.set_thumbnail(url=m.display_avatar.url)
        embed.add_field(name="Level", value=str(level))
        embed.add_field(name="Rank", value=f"#{position}")
        embed.add_field(name="Total XP", value=f"{xp:,}")
        embed.add_field(name="Progress", value=f"`{progress_bar(into / need)}` {into:,}/{need:,} XP", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Server leaderboard")
    @app_commands.guild_only()
    @app_commands.choices(board=[app_commands.Choice(name="Levels", value="xp"), app_commands.Choice(name="Economy", value="balance")])
    async def leaderboard(self, interaction: discord.Interaction, board: app_commands.Choice[str] | None = None, page: app_commands.Range[int, 1, 50] = 1):
        kind = board.value if board else "xp"
        offset = (page - 1) * 10
        if kind == "xp":
            rows = await self.bot.db.fetchall("SELECT user_id, xp, level FROM levels WHERE guild_id=? ORDER BY xp DESC LIMIT 10 OFFSET ?", (interaction.guild_id, offset))
            lines = [f"**{offset + i + 1}.** <@{r['user_id']}> — level {r['level']} · {r['xp']:,} XP" for i, r in enumerate(rows)]
            title = "Level leaderboard"
        else:
            e = await self.bot.settings.get(interaction.guild_id, "economy")
            rows = await self.bot.db.fetchall("SELECT user_id, balance FROM economy WHERE guild_id=? ORDER BY balance DESC LIMIT 10 OFFSET ?", (interaction.guild_id, offset))
            lines = [f"**{offset + i + 1}.** <@{r['user_id']}> — {e['currency_symbol']} {r['balance']:,}" for i, r in enumerate(rows)]
            title = "Richest members"
        embed = brand_embed(title, "\n".join(lines) or "Nobody here yet.")
        embed.set_footer(text=f"Page {page} · Full leaderboard: {self.bot.config.public_url}/leaderboard/{interaction.guild_id}")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    xp = app_commands.Group(name="xp", description="Manage member XP", guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    @xp.command(name="set", description="Set a member's level")
    @manager_check()
    async def xp_set(self, interaction: discord.Interaction, member: discord.Member, level: app_commands.Range[int, 0, 1000]):
        xp = total_xp_for_level(level)
        await self.bot.db.execute(
            """INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=excluded.xp, level=excluded.level""",
            (interaction.guild_id, member.id, xp, level))
        s = await self.bot.settings.get(interaction.guild_id, "leveling")
        await self.sync_rewards(member, level, s)
        await interaction.response.send_message(embed=ok(f"{member.mention} is now level **{level}**."))

    @xp.command(name="add", description="Give (or take, with a negative number) XP")
    @manager_check()
    async def xp_add(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, -1_000_000, 1_000_000]):
        await self.add_xp(member, amount, None)
        await interaction.response.send_message(embed=ok(f"Adjusted {member.mention}'s XP by **{amount:+,}**."))

    @xp.command(name="reset", description="Reset a member's XP")
    @manager_check()
    async def xp_reset(self, interaction: discord.Interaction, member: discord.Member):
        await self.bot.db.execute("DELETE FROM levels WHERE guild_id=? AND user_id=?", (interaction.guild_id, member.id))
        await interaction.response.send_message(embed=ok(f"Reset {member.mention}'s XP."))


async def setup(bot):
    await bot.add_cog(Leveling(bot))
