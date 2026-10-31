"""Utility: reminders, custom commands, scheduled messages, announcements, saved embeds."""
from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from nexora.bot.helpers import has_any_role, manager_check
from nexora.core.permissions import allowed_mentions_for, can_mention_everyone
from nexora.core.utils import brand_embed, color_int, embed_from_dict, embed_has_content, err, human_duration, link_buttons_view, ok, parse_duration, render, truncate
from nexora.db import from_db_time, jload, to_db_time, utcnow


class AnnounceModal(discord.ui.Modal, title="Announcement"):
    a_title = discord.ui.TextInput(label="Title", max_length=256, required=False)
    body = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=4000)
    color = discord.ui.TextInput(label="Color (hex)", default="#7C3AED", max_length=7, required=False)
    image = discord.ui.TextInput(label="Image URL (optional)", required=False, max_length=500)

    def __init__(self, channel: discord.TextChannel, ping: discord.Role | None):
        super().__init__()
        self.channel, self.ping = channel, ping

    async def on_submit(self, interaction: discord.Interaction):
        embed = brand_embed(self.a_title.value or None, self.body.value, color_int(self.color.value))
        if self.image.value.startswith(("http://", "https://")):
            embed.set_image(url=self.image.value)
        embed.set_footer(text=f"Announced by {interaction.user.display_name}")
        await self.channel.send(content=self.ping.mention if self.ping else None, embed=embed,
                                allowed_mentions=discord.AllowedMentions(roles=[self.ping] if self.ping and not self.ping.is_default() else False,
                                                                         everyone=bool(self.ping and self.ping.is_default()), users=False))
        await interaction.response.send_message(embed=ok(f"Announcement sent to {self.channel.mention}."), ephemeral=True)


class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.reminder_loop.start()
        self.schedule_loop.start()

    def cog_unload(self):
        self.reminder_loop.cancel()
        self.schedule_loop.cancel()

    # ------------------------------------------------------------ reminders
    @app_commands.command(description="Set a reminder")
    @app_commands.describe(when="e.g. 10m, 2h, 1d", text="What to remind you about")
    async def remind(self, interaction: discord.Interaction, when: str, text: app_commands.Range[str, 1, 1000], here: bool = False):
        d = parse_duration(when)
        if not d or d > timedelta(days=365):
            return await interaction.response.send_message(embed=err("Use a duration like `10m`, `2h`, `3d` (max 1 year)."), ephemeral=True)
        mine = await self.bot.db.fetchval("SELECT COUNT(*) FROM reminders WHERE user_id=? AND done=0", (interaction.user.id,), 0)
        if mine >= 25:
            return await interaction.response.send_message(embed=err("You have 25 pending reminders already."), ephemeral=True)
        at = utcnow() + d
        await self.bot.db.execute(
            "INSERT INTO reminders (user_id, guild_id, channel_id, content, remind_at) VALUES (?, ?, ?, ?, ?)",
            (interaction.user.id, interaction.guild_id, interaction.channel_id if here else None, text, to_db_time(at)))
        await interaction.response.send_message(embed=ok(f"I'll remind you {discord.utils.format_dt(at, 'R')}" + (" in this channel." if here else " by DM.")), ephemeral=True)

    @app_commands.command(description="List or cancel your reminders")
    async def reminders(self, interaction: discord.Interaction, cancel_id: int | None = None):
        if cancel_id:
            n = await self.bot.db.execute_rowcount("DELETE FROM reminders WHERE id=? AND user_id=? AND done=0", (cancel_id, interaction.user.id))
            return await interaction.response.send_message(embed=ok("Reminder cancelled.") if n else err("No such reminder."), ephemeral=True)
        rows = await self.bot.db.fetchall("SELECT * FROM reminders WHERE user_id=? AND done=0 ORDER BY remind_at LIMIT 25", (interaction.user.id,))
        lines = [f"`#{r['id']}` {discord.utils.format_dt(from_db_time(r['remind_at']), 'R')} — {truncate(r['content'], 80)}" for r in rows]
        await interaction.response.send_message(embed=brand_embed("Your reminders", "\n".join(lines) or "No reminders."), ephemeral=True)

    @tasks.loop(seconds=20)
    async def reminder_loop(self):
        for r in await self.bot.db.fetchall("SELECT * FROM reminders WHERE done=0 AND remind_at <= ? LIMIT 50", (to_db_time(utcnow()),)):
            await self.bot.db.execute("UPDATE reminders SET done=1 WHERE id=?", (r["id"],))
            embed = brand_embed("⏰ Reminder", r["content"])
            embed.add_field(name="Set", value=discord.utils.format_dt(from_db_time(r["created_at"]), "R"))
            sent = False
            if r["channel_id"]:
                ch = self.bot.get_channel(r["channel_id"])
                if isinstance(ch, discord.abc.Messageable):
                    try:
                        await ch.send(f"<@{r['user_id']}>", embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
                        sent = True
                    except discord.HTTPException:
                        pass
            if not sent:
                try:
                    user = self.bot.get_user(r["user_id"]) or await self.bot.fetch_user(r["user_id"])
                    await user.send(embed=embed)
                except discord.HTTPException:
                    pass

    # ------------------------------------------------------ custom commands
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot or not message.content:
            return
        general = await self.bot.settings.get(message.guild.id, "general")
        prefix = general["prefix"] or "!"
        if not message.content.startswith(prefix):
            return
        name = message.content[len(prefix):].split(" ", 1)[0].lower()
        if not name:
            return
        cmd = await self.bot.db.fetchone("SELECT * FROM custom_commands WHERE guild_id=? AND name=? AND enabled=1", (message.guild.id, name))
        if not cmd:
            return
        roles = jload(cmd["required_role_ids"], [])
        if roles and not await has_any_role(message.author, roles):  # type: ignore[arg-type]
            return
        args = message.content[len(prefix) + len(name):].strip()
        content = render(cmd["response"], message.author, message.guild, args=args, channel=message.channel.mention) if cmd["response"] else None
        embed_data = jload(cmd["embed"], None)
        embed = embed_from_dict(embed_data, message.author, message.guild) if embed_has_content(embed_data) else None
        view = link_buttons_view(embed_data.get("buttons")) if embed_data else None
        if not content and not embed:
            return
        try:
            await message.channel.send(content=content, embed=embed, view=view or discord.utils.MISSING,
                                       allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            if cmd["delete_trigger"]:
                await message.delete()
        except discord.HTTPException:
            return
        await self.bot.db.execute("UPDATE custom_commands SET uses = uses + 1 WHERE id=?", (cmd["id"],))
        await self.bot.track_feature(message.guild.id, "custom_command")

    # ---------------------------------------------------- scheduled messages
    async def send_payload(self, channel: discord.abc.Messageable, content: str | None, embed_data: dict | None, guild: discord.Guild | None = None,
                           actor: discord.Member | None = None) -> discord.Message:
        """Send a saved/scheduled message. Pings are limited to what `actor` (the person behind it) could ping in Discord."""
        embed = embed_from_dict(embed_data, None, guild) if embed_has_content(embed_data) else None
        view = link_buttons_view(embed_data.get("buttons")) if embed_data else None
        return await channel.send(content=render(content, None, guild) if content else None, embed=embed, view=view or discord.utils.MISSING,
                                  allowed_mentions=allowed_mentions_for(actor, guild))

    @tasks.loop(seconds=30)
    async def schedule_loop(self):
        for row in await self.bot.db.fetchall("SELECT * FROM scheduled_messages WHERE enabled=1 AND next_run_at <= ?", (to_db_time(utcnow()),)):
            guild = self.bot.get_guild(row["guild_id"])
            ch = guild.get_channel(row["channel_id"]) if guild else None
            if row["repeat_hours"]:
                nxt = from_db_time(row["next_run_at"]) + timedelta(hours=row["repeat_hours"])
                while nxt <= utcnow():
                    nxt += timedelta(hours=row["repeat_hours"])
                await self.bot.db.execute("UPDATE scheduled_messages SET next_run_at=? WHERE id=?", (to_db_time(nxt), row["id"]))
            else:
                await self.bot.db.execute("UPDATE scheduled_messages SET enabled=0 WHERE id=?", (row["id"],))
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    actor = guild.get_member(row["created_by"]) if row["created_by"] else None
                    await self.send_payload(ch, row["content"], jload(row["embed"], None), guild, actor=actor)
                except discord.HTTPException as exc:
                    await self.bot.db.log_error("scheduled_message", f"#{row['id']}: {exc}", guild_id=row["guild_id"])

    @reminder_loop.before_loop
    @schedule_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ----------------------------------------------------------- announce
    @app_commands.command(description="Send an announcement embed")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel, ping: discord.Role | None = None):
        perms = channel.permissions_for(interaction.user)  # type: ignore[arg-type]
        if not perms.send_messages:
            return await interaction.response.send_message(embed=err("You can't send messages in that channel."), ephemeral=True)
        if ping and (ping.is_default() or not ping.mentionable) and not can_mention_everyone(interaction.user):  # type: ignore[arg-type]
            return await interaction.response.send_message(embed=err("You need the Mention @everyone permission to ping that role."), ephemeral=True)
        await interaction.response.send_modal(AnnounceModal(channel, ping))

    @app_commands.command(name="embed", description="Send a saved embed (create them in the dashboard's embed builder)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def send_embed(self, interaction: discord.Interaction, name: str, channel: discord.TextChannel | None = None):
        row = await self.bot.db.fetchone("SELECT * FROM saved_embeds WHERE guild_id=? AND name=?", (interaction.guild_id, name))
        if not row:
            return await interaction.response.send_message(embed=err("No saved embed with that name."), ephemeral=True)
        ch = channel or interaction.channel
        if not ch.permissions_for(interaction.user).send_messages:  # type: ignore[union-attr, arg-type]
            return await interaction.response.send_message(embed=err("You can't send messages in that channel."), ephemeral=True)
        data = jload(row["data"], {})
        try:
            await self.send_payload(ch, data.get("content"), data, interaction.guild, actor=interaction.user)  # type: ignore[arg-type]
        except discord.HTTPException:
            return await interaction.response.send_message(embed=err("I couldn't send messages in that channel."), ephemeral=True)
        await interaction.response.send_message(embed=ok(f"Sent to {ch.mention}."), ephemeral=True)

    @send_embed.autocomplete("name")
    async def _embed_ac(self, interaction: discord.Interaction, current: str):
        rows = await self.bot.db.fetchall("SELECT name FROM saved_embeds WHERE guild_id=? ORDER BY name LIMIT 25", (interaction.guild_id,))
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows if current.lower() in r["name"].lower()]


async def setup(bot):
    await bot.add_cog(Utility(bot))
