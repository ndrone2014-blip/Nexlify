"""Welcome / goodbye messages, welcome DMs and auto roles."""
from __future__ import annotations

import asyncio

import discord
from discord.ext import commands

from nexora.core.utils import brand_embed, color_int, render


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def build_message(self, guild: discord.Guild, member: discord.abc.User, s: dict, kind: str) -> dict:
        general = await self.bot.settings.get(guild.id, "general")
        text = render(s[f"{kind}_message"], member, guild)
        if not s[f"{kind}_embed"]:
            return {"content": text}
        embed = brand_embed(render(s.get(f"{kind}_title", ""), member, guild) or None, text, color_int(general["embed_color"]))
        if kind == "welcome":
            if s["welcome_thumbnail_avatar"]:
                embed.set_thumbnail(url=member.display_avatar.url)
            if s["welcome_image_url"]:
                embed.set_image(url=s["welcome_image_url"])
        else:
            embed.set_thumbnail(url=member.display_avatar.url)
        out: dict = {"embed": embed}
        if kind == "welcome":
            out["content"] = member.mention  # ping outside the embed so the member gets notified
            if s["welcome_button_label"] and s["welcome_button_url"]:
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label=s["welcome_button_label"][:80], url=s["welcome_button_url"]))
                out["view"] = view
        return out

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = member.guild
        s = await self.bot.settings.get(g.id, "welcome")
        roles = s["bot_autorole_ids"] if member.bot else s["autorole_ids"]
        if roles:
            asyncio.create_task(self._give_roles(member, roles, s["autorole_delay_seconds"]))
        if member.bot:
            return
        if s["welcome_enabled"] and s["welcome_channel_id"]:
            ch = g.get_channel(s["welcome_channel_id"])
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    await ch.send(**await self.build_message(g, member, s, "welcome"),
                                  allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                except discord.HTTPException:
                    pass
        if s["dm_enabled"]:
            try:
                await member.send(render(s["dm_message"], member, g))
            except discord.HTTPException:
                pass
        self.bot.trigger(g, "member_join", member=member)

    async def _give_roles(self, member: discord.Member, role_ids: list[int], delay: int):
        if delay:
            await asyncio.sleep(delay)
        if member.pending and not delay:
            # Membership screening: roles can't be given until rules are accepted; on_member_update handles it.
            return
        roles = [r for r in (member.guild.get_role(i) for i in role_ids) if r and r < member.guild.me.top_role]
        if roles and member.guild.get_member(member.id):
            try:
                await member.add_roles(*roles, reason="Nexora auto role")
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.pending and not after.pending:
            s = await self.bot.settings.get(after.guild.id, "welcome")
            if s["autorole_ids"]:
                await self._give_roles(after, s["autorole_ids"], 0)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        g = member.guild
        s = await self.bot.settings.get(g.id, "welcome")
        if not member.bot and s["goodbye_enabled"] and s["goodbye_channel_id"]:
            ch = g.get_channel(s["goodbye_channel_id"])
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    await ch.send(**await self.build_message(g, member, s, "goodbye"), allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
        self.bot.trigger(g, "member_leave", member=member)


async def setup(bot):
    await bot.add_cog(Welcome(bot))
