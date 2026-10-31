"""Moderation: cases, warnings with escalation, temp bans, notes, appeals, staff stats."""
from __future__ import annotations

import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from nexora.bot.helpers import can_act_on, log_event
from nexora.core.utils import DANGER, SUCCESS, WARNING, brand_embed, err, human_duration, ok, parse_duration, truncate
from nexora.db import from_db_time, to_db_time, utcnow

ACTION_COLORS = {"ban": DANGER, "tempban": DANGER, "kick": DANGER, "timeout": WARNING, "warn": WARNING,
                 "unban": SUCCESS, "untimeout": SUCCESS, "purge": WARNING, "lock": WARNING, "unlock": SUCCESS, "note": 0x6366F1}
MAX_TIMEOUT = timedelta(days=28)


class AppealButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:appeal:(?P<action>accept|deny):(?P<id>\d+)"):
    def __init__(self, action: str, appeal_id: int):
        super().__init__(
            discord.ui.Button(
                label="Accept" if action == "accept" else "Deny",
                style=discord.ButtonStyle.success if action == "accept" else discord.ButtonStyle.danger,
                custom_id=f"nx:appeal:{action}:{appeal_id}",
            )
        )
        self.action = action
        self.appeal_id = appeal_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.ban_members:  # type: ignore[union-attr]
            return await interaction.response.send_message(embed=err("You need Ban Members to review appeals."), ephemeral=True)
        cog: Moderation = interaction.client.get_cog("Moderation")  # type: ignore[assignment]
        result = await cog.resolve_appeal(interaction.guild, self.appeal_id, self.action == "accept", interaction.user, None)
        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0]
            embed.color = SUCCESS if self.action == "accept" else DANGER
            embed.add_field(name="Decision", value=f"{'Accepted' if self.action == 'accept' else 'Denied'} by {interaction.user.mention}\n{result}", inline=False)
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.send_message(result, ephemeral=True)


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_dynamic_items(AppealButton)
        self.expiry_loop.start()

    def cog_unload(self):
        self.expiry_loop.cancel()

    # ============================================================ services
    async def create_case(
        self,
        guild: discord.Guild,
        action: str,
        user: discord.abc.User | int,
        moderator: discord.abc.User | None,
        reason: str | None,
        duration: timedelta | None = None,
    ) -> int:
        user_id = user if isinstance(user, int) else user.id
        number = await self.bot.db.next_number("mod_cases", "case_number", guild.id)
        expires = to_db_time(utcnow() + duration) if duration else None
        await self.bot.db.execute(
            """INSERT INTO mod_cases (guild_id, case_number, action, user_id, moderator_id, reason, duration_seconds, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild.id, number, action, user_id, moderator.id if moderator else None, reason,
             int(duration.total_seconds()) if duration else None, expires),
        )
        await self.bot.bump_stat(guild.id, "mod_actions")
        if moderator and not moderator.bot:
            await self.bot.track_staff(guild.id, moderator.id)
        name = str(user) if not isinstance(user, int) else f"<@{user}>"
        embed = brand_embed(f"Case #{number} · {action.title()}", color=ACTION_COLORS.get(action, WARNING))
        embed.add_field(name="User", value=f"<@{user_id}> ({user_id})")
        embed.add_field(name="Moderator", value=moderator.mention if moderator else "Nexora (automatic)")
        if duration:
            embed.add_field(name="Duration", value=human_duration(duration.total_seconds()))
        embed.add_field(name="Reason", value=truncate(reason, 1024) or "No reason given", inline=False)
        await log_event(self.bot, guild, "moderation", f"case_{action}", f"Case #{number}: {action} {name} — {reason or 'no reason'}",
                        embed, user_id=user_id, moderator_id=moderator.id if moderator else None,
                        data={"case": number, "reason": reason, "duration": duration.total_seconds() if duration else None})
        return number

    async def dm_user(self, guild: discord.Guild, user: discord.abc.User, action: str, reason: str | None,
                      moderator: discord.abc.User | None, duration: timedelta | None = None, case: int | None = None) -> bool:
        s = await self.bot.settings.get(guild.id, "moderation")
        if not s["dm_users"]:
            return False
        verbs = {"ban": "banned from", "tempban": "temporarily banned from", "kick": "kicked from", "timeout": "timed out in",
                 "warn": "warned in", "untimeout": "un-timed-out in", "unban": "unbanned from"}
        embed = brand_embed(f"You were {verbs.get(action, action)} {guild.name}", color=ACTION_COLORS.get(action, WARNING))
        embed.add_field(name="Reason", value=reason or "No reason given", inline=False)
        if duration:
            embed.add_field(name="Duration", value=human_duration(duration.total_seconds()))
        if s["show_moderator"] and moderator:
            embed.add_field(name="Moderator", value=str(moderator))
        if case:
            embed.add_field(name="Case", value=f"#{case}")
        if s["appeals_enabled"] and action in {"ban", "tempban", "timeout", "warn"}:
            embed.add_field(name="Appeal", value=f"Use `/appeal server_id:{guild.id}` in a DM with me to appeal.", inline=False)
        try:
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def active_warning_count(self, guild_id: int, user_id: int) -> int:
        s = await self.bot.settings.get(guild_id, "moderation")
        if s["warn_expiry_days"]:
            return await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=? AND active=1 AND created_at > datetime('now', ?)",
                (guild_id, user_id, f"-{s['warn_expiry_days']} days"), 0)
        return await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=? AND active=1", (guild_id, user_id), 0)

    async def issue_warning(self, guild: discord.Guild, member: discord.Member, moderator: discord.abc.User | None, reason: str | None) -> tuple[int, int, str | None]:
        """Warn a member, then apply escalation. Returns (case, active_count, escalation_result)."""
        case = await self.create_case(guild, "warn", member, moderator, reason)
        await self.bot.db.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, case_number) VALUES (?, ?, ?, ?, ?)",
            (guild.id, member.id, moderator.id if moderator else None, reason, case),
        )
        await self.dm_user(guild, member, "warn", reason, moderator, case=case)
        count = await self.active_warning_count(guild.id, member.id)
        result = await self.escalate(guild, member, count)
        self.bot.trigger(guild, "member_warned", member=member, reason=reason or "", warnings=count)
        return case, count, result

    async def escalate(self, guild: discord.Guild, member: discord.Member, count: int) -> str | None:
        s = await self.bot.settings.get(guild.id, "moderation")
        if not s["escalation_enabled"]:
            return None
        rule = next((r for r in s["escalation"] if r["warnings"] == count), None)
        if not rule:
            return None
        reason = f"Automatic: reached {count} warnings"
        me = guild.me
        try:
            if rule["action"] == "timeout":
                d = min(timedelta(minutes=rule["duration_minutes"] or 60), MAX_TIMEOUT)
                await member.timeout(d, reason=reason)
                await self.create_case(guild, "timeout", member, me, reason, d)
                await self.dm_user(guild, member, "timeout", reason, None, d)
                return f"timed out for {human_duration(d.total_seconds())}"
            if rule["action"] == "kick":
                await self.dm_user(guild, member, "kick", reason, None)
                await member.kick(reason=reason)
                await self.create_case(guild, "kick", member, me, reason)
                return "kicked"
            if rule["action"] == "ban":
                d = timedelta(minutes=rule["duration_minutes"]) if rule["duration_minutes"] else None
                await self.dm_user(guild, member, "tempban" if d else "ban", reason, None, d)
                await member.ban(reason=reason, delete_message_seconds=0)
                await self.create_case(guild, "tempban" if d else "ban", member, me, reason, d)
                return "banned" + (f" for {human_duration(d.total_seconds())}" if d else "")
        except discord.HTTPException as exc:
            return f"escalation failed ({exc.text or 'missing permissions'})"
        return None

    async def resolve_appeal(self, guild: discord.Guild, appeal_id: int, accept: bool, reviewer: discord.abc.User, response: str | None) -> str:
        appeal = await self.bot.db.fetchone("SELECT * FROM appeals WHERE id=? AND guild_id=?", (appeal_id, guild.id))
        if not appeal:
            return "Appeal not found."
        if appeal["status"] != "pending":
            return f"Appeal already {appeal['status']}."
        await self.bot.db.execute(
            "UPDATE appeals SET status=?, reviewer_id=?, response=?, resolved_at=datetime('now') WHERE id=?",
            ("accepted" if accept else "denied", reviewer.id, response, appeal_id),
        )
        detail = ""
        if accept:
            case = await self.bot.db.fetchone("SELECT * FROM mod_cases WHERE guild_id=? AND case_number=?", (guild.id, appeal["case_number"])) if appeal["case_number"] else None
            action = case["action"] if case else None
            try:
                if action in {"ban", "tempban"}:
                    await guild.unban(discord.Object(appeal["user_id"]), reason=f"Appeal #{appeal_id} accepted by {reviewer}")
                    await self.create_case(guild, "unban", appeal["user_id"], reviewer, f"Appeal #{appeal_id} accepted")
                    detail = "User unbanned."
                elif action == "timeout":
                    m = guild.get_member(appeal["user_id"])
                    if m:
                        await m.timeout(None, reason=f"Appeal #{appeal_id} accepted")
                    detail = "Timeout removed."
                elif action == "warn":
                    await self.bot.db.execute("UPDATE warnings SET active=0 WHERE guild_id=? AND case_number=?", (guild.id, appeal["case_number"]))
                    detail = "Warning removed."
                if case:
                    await self.bot.db.execute("UPDATE mod_cases SET active=0 WHERE id=?", (case["id"],))
            except discord.HTTPException as exc:
                detail = f"Could not reverse the punishment automatically: {exc.text}"
        user = self.bot.get_user(appeal["user_id"])
        if user:
            try:
                await user.send(embed=brand_embed(
                    f"Your appeal in {guild.name} was {'accepted' if accept else 'denied'}",
                    response or None, SUCCESS if accept else DANGER))
            except discord.HTTPException:
                pass
        await self.bot.track_staff(guild.id, reviewer.id)
        return detail or ("Accepted." if accept else "Denied.")

    async def _reason_ok(self, interaction: discord.Interaction, reason: str | None) -> bool:
        s = await self.bot.settings.get(interaction.guild_id, "moderation")
        if s["require_reason"] and not reason:
            await interaction.response.send_message(embed=err("A reason is required on this server."), ephemeral=True)
            return False
        return True

    async def _check_target(self, interaction: discord.Interaction, member: discord.Member) -> bool:
        problem = can_act_on(interaction.user, member)  # type: ignore[arg-type]
        if problem:
            await interaction.response.send_message(embed=err(problem), ephemeral=True)
            return False
        return True

    # ============================================================ commands
    @app_commands.command(description="Ban a user (optionally temporary)")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.describe(duration="e.g. 7d, 12h — leave empty for permanent", delete_days="Delete their messages from the last N days (0-7)")
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: str | None = None,
                  duration: str | None = None, delete_days: app_commands.Range[int, 0, 7] = 0):
        if not await self._reason_ok(interaction, reason):
            return
        member = interaction.guild.get_member(user.id)
        if member and not await self._check_target(interaction, member):
            return
        d = parse_duration(duration) if duration else None
        if duration and not d:
            return await interaction.response.send_message(embed=err("Invalid duration. Use e.g. `7d`, `12h`, `30m`."), ephemeral=True)
        action = "tempban" if d else "ban"
        await interaction.response.defer()
        dmed = await self.dm_user(interaction.guild, user, action, reason, interaction.user, d) if member else False
        await interaction.guild.ban(user, reason=f"{interaction.user}: {reason or 'No reason'}", delete_message_seconds=delete_days * 86400)
        case = await self.create_case(interaction.guild, action, user, interaction.user, reason, d)
        await interaction.followup.send(embed=ok(f"**{user}** was banned{' for ' + human_duration(d.total_seconds()) if d else ''}. (Case #{case}{', DM sent' if dmed else ''})"))

    @app_commands.command(description="Unban a user by ID")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str | None = None):
        if not user_id.isdigit():
            return await interaction.response.send_message(embed=err("Provide a numeric user ID."), ephemeral=True)
        try:
            await interaction.guild.unban(discord.Object(int(user_id)), reason=f"{interaction.user}: {reason or 'No reason'}")
        except discord.NotFound:
            return await interaction.response.send_message(embed=err("That user isn't banned."), ephemeral=True)
        await self.bot.db.execute("UPDATE mod_cases SET active=0 WHERE guild_id=? AND user_id=? AND action IN ('ban','tempban')", (interaction.guild_id, int(user_id)))
        case = await self.create_case(interaction.guild, "unban", int(user_id), interaction.user, reason)
        await interaction.response.send_message(embed=ok(f"<@{user_id}> was unbanned. (Case #{case})"))

    @app_commands.command(description="Kick a member")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
        if not await self._reason_ok(interaction, reason) or not await self._check_target(interaction, member):
            return
        dmed = await self.dm_user(interaction.guild, member, "kick", reason, interaction.user)
        await member.kick(reason=f"{interaction.user}: {reason or 'No reason'}")
        case = await self.create_case(interaction.guild, "kick", member, interaction.user, reason)
        await interaction.response.send_message(embed=ok(f"**{member}** was kicked. (Case #{case}{', DM sent' if dmed else ''})"))

    @app_commands.command(description="Time out a member")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @app_commands.describe(duration="e.g. 10m, 2h, 1d (max 28d). Empty = server default")
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str | None = None, reason: str | None = None):
        if not await self._reason_ok(interaction, reason) or not await self._check_target(interaction, member):
            return
        s = await self.bot.settings.get(interaction.guild_id, "moderation")
        d = parse_duration(duration) if duration else timedelta(minutes=s["default_timeout_minutes"])
        if not d:
            return await interaction.response.send_message(embed=err("Invalid duration."), ephemeral=True)
        d = min(d, MAX_TIMEOUT)
        await member.timeout(d, reason=f"{interaction.user}: {reason or 'No reason'}")
        case = await self.create_case(interaction.guild, "timeout", member, interaction.user, reason, d)
        await self.dm_user(interaction.guild, member, "timeout", reason, interaction.user, d, case)
        await interaction.response.send_message(embed=ok(f"**{member}** was timed out for {human_duration(d.total_seconds())}. (Case #{case})"))

    @app_commands.command(description="Remove a member's timeout")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
        if not member.is_timed_out():
            return await interaction.response.send_message(embed=err("That member isn't timed out."), ephemeral=True)
        await member.timeout(None, reason=f"{interaction.user}: {reason or 'No reason'}")
        case = await self.create_case(interaction.guild, "untimeout", member, interaction.user, reason)
        await interaction.response.send_message(embed=ok(f"Removed **{member}**'s timeout. (Case #{case})"))

    @app_commands.command(description="Warn a member")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
        if not await self._reason_ok(interaction, reason) or not await self._check_target(interaction, member):
            return
        await interaction.response.defer()
        case, count, result = await self.issue_warning(interaction.guild, member, interaction.user, reason)  # type: ignore[misc]
        msg = f"**{member}** was warned. They now have **{count}** active warning(s). (Case #{case})"
        if result:
            msg += f"\n⚡ Escalation: {result}"
        await interaction.followup.send(embed=ok(msg))

    @app_commands.command(description="List a member's warnings")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.User):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM warnings WHERE guild_id=? AND user_id=? AND active=1 ORDER BY id DESC LIMIT 25", (interaction.guild_id, member.id))
        embed = brand_embed(f"Warnings for {member}", color=WARNING)
        if not rows:
            embed.description = "No active warnings."
        for r in rows:
            embed.add_field(name=f"ID {r['id']} · Case #{r['case_number']} · {r['created_at'][:10]}",
                            value=f"{truncate(r['reason'], 200) or 'No reason'} — <@{r['moderator_id']}>", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Clear all of a member's warnings")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.User, reason: str | None = None):
        n = await self.bot.db.execute_rowcount("UPDATE warnings SET active=0 WHERE guild_id=? AND user_id=? AND active=1", (interaction.guild_id, member.id))
        await log_event(self.bot, interaction.guild, "moderation", "clear_warnings", f"{interaction.user} cleared {n} warnings for {member}",
                        brand_embed("Warnings cleared", f"{interaction.user.mention} cleared **{n}** warnings for {member.mention}\nReason: {reason or '—'}", SUCCESS),
                        user_id=member.id, moderator_id=interaction.user.id)
        await self.bot.track_staff(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(embed=ok(f"Cleared **{n}** warning(s) for {member}."))

    @app_commands.command(description="Delete a single warning by its ID")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def delwarn(self, interaction: discord.Interaction, warning_id: int):
        n = await self.bot.db.execute_rowcount("UPDATE warnings SET active=0 WHERE guild_id=? AND id=? AND active=1", (interaction.guild_id, warning_id))
        if not n:
            return await interaction.response.send_message(embed=err("No active warning with that ID."), ephemeral=True)
        await self.bot.track_staff(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(embed=ok(f"Warning {warning_id} removed."))

    @app_commands.command(description="Bulk delete messages")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    @app_commands.describe(amount="Messages to scan (1-500)", user="Only delete this user's messages", contains="Only messages containing this text", bots="Only bot messages")
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 500],
                    user: discord.User | None = None, contains: str | None = None, bots: bool = False):
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return await interaction.response.send_message(embed=err("Can't purge here."), ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        def check(m: discord.Message) -> bool:
            if m.pinned:
                return False
            if user and m.author.id != user.id:
                return False
            if bots and not m.author.bot:
                return False
            if contains and contains.lower() not in m.content.lower():
                return False
            return True

        deleted = await channel.purge(limit=amount, check=check, reason=f"Purge by {interaction.user}")
        await self.bot.bump_stat(interaction.guild_id, "mod_actions")
        await self.bot.track_staff(interaction.guild_id, interaction.user.id)
        await log_event(self.bot, interaction.guild, "moderation", "purge", f"{interaction.user} purged {len(deleted)} messages in #{channel.name}",
                        brand_embed("Messages purged", f"{interaction.user.mention} deleted **{len(deleted)}** messages in {channel.mention}", WARNING),
                        moderator_id=interaction.user.id, channel_id=channel.id, data={"count": len(deleted)})
        await interaction.followup.send(embed=ok(f"Deleted **{len(deleted)}** messages. (Discord can't bulk delete messages older than 14 days.)"), ephemeral=True)

    @app_commands.command(description="Set a channel's slowmode")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600], channel: discord.TextChannel | None = None):
        ch = channel or interaction.channel
        await ch.edit(slowmode_delay=seconds, reason=f"Slowmode by {interaction.user}")
        await self.bot.track_staff(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(embed=ok(f"Slowmode in {ch.mention} set to **{seconds}s**." if seconds else f"Slowmode disabled in {ch.mention}."))

    @app_commands.command(description="Lock a channel so @everyone can't send messages")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None, reason: str | None = None):
        ch = channel or interaction.channel
        ow = ch.overwrites_for(interaction.guild.default_role)
        ow.send_messages = False
        ow.send_messages_in_threads = False
        ow.add_reactions = False
        await ch.set_permissions(interaction.guild.default_role, overwrite=ow, reason=f"Lock by {interaction.user}: {reason}")
        await self.create_case(interaction.guild, "lock", interaction.user, interaction.user, f"#{ch.name}: {reason or 'No reason'}")
        await ch.send(embed=brand_embed("🔒 Channel locked", reason or None, WARNING))
        await interaction.response.send_message(embed=ok(f"{ch.mention} locked."), ephemeral=True)

    @app_commands.command(description="Unlock a channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        ch = channel or interaction.channel
        ow = ch.overwrites_for(interaction.guild.default_role)
        ow.send_messages = None
        ow.send_messages_in_threads = None
        ow.add_reactions = None
        await ch.set_permissions(interaction.guild.default_role, overwrite=None if ow.is_empty() else ow, reason=f"Unlock by {interaction.user}")
        await self.create_case(interaction.guild, "unlock", interaction.user, interaction.user, f"#{ch.name}")
        await ch.send(embed=brand_embed("🔓 Channel unlocked", color=SUCCESS))
        await interaction.response.send_message(embed=ok(f"{ch.mention} unlocked."), ephemeral=True)

    @app_commands.command(description="Change or reset a member's nickname")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_nicknames=True)
    @app_commands.checks.has_permissions(manage_nicknames=True)
    @app_commands.checks.bot_has_permissions(manage_nicknames=True)
    async def nickname(self, interaction: discord.Interaction, member: discord.Member, nickname: app_commands.Range[str, 1, 32] | None = None):
        if member.id != interaction.user.id and not await self._check_target(interaction, member):
            return
        await member.edit(nick=nickname, reason=f"By {interaction.user}")
        await self.bot.track_staff(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(embed=ok(f"Nickname {'set to **' + nickname + '**' if nickname else 'reset'} for {member.mention}."))

    @app_commands.command(description="View a moderation case")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def case(self, interaction: discord.Interaction, number: int, new_reason: str | None = None):
        c = await self.bot.db.fetchone("SELECT * FROM mod_cases WHERE guild_id=? AND case_number=?", (interaction.guild_id, number))
        if not c:
            return await interaction.response.send_message(embed=err("Case not found."), ephemeral=True)
        if new_reason:
            await self.bot.db.execute("UPDATE mod_cases SET reason=? WHERE id=?", (new_reason, c["id"]))
            await self.bot.db.execute("UPDATE warnings SET reason=? WHERE guild_id=? AND case_number=?", (new_reason, interaction.guild_id, number))
            c["reason"] = new_reason
        embed = brand_embed(f"Case #{number} · {c['action'].title()}", color=ACTION_COLORS.get(c["action"], WARNING))
        embed.add_field(name="User", value=f"<@{c['user_id']}> ({c['user_id']})")
        embed.add_field(name="Moderator", value=f"<@{c['moderator_id']}>" if c["moderator_id"] else "Automatic")
        embed.add_field(name="Date", value=c["created_at"] + " UTC")
        if c["duration_seconds"]:
            embed.add_field(name="Duration", value=human_duration(c["duration_seconds"]))
        embed.add_field(name="Active", value="Yes" if c["active"] else "No")
        embed.add_field(name="Reason", value=c["reason"] or "No reason", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="A user's full moderation history")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def history(self, interaction: discord.Interaction, user: discord.User):
        rows = await self.bot.db.fetchall("SELECT * FROM mod_cases WHERE guild_id=? AND user_id=? ORDER BY case_number DESC LIMIT 20", (interaction.guild_id, user.id))
        notes = await self.bot.db.fetchall("SELECT * FROM mod_notes WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 5", (interaction.guild_id, user.id))
        embed = brand_embed(f"History for {user}")
        embed.set_thumbnail(url=user.display_avatar.url)
        lines = [f"`#{r['case_number']}` **{r['action']}** · {r['created_at'][:10]} · {truncate(r['reason'], 60) or '—'}" for r in rows]
        embed.description = "\n".join(lines) or "No cases."
        if notes:
            embed.add_field(name="Notes", value="\n".join(f"• {truncate(n['note'], 100)} — <@{n['moderator_id']}>" for n in notes), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Add a private moderator note to a user (or list notes if text is empty)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def note(self, interaction: discord.Interaction, user: discord.User, text: str | None = None):
        if text:
            await self.bot.db.execute("INSERT INTO mod_notes (guild_id, user_id, moderator_id, note) VALUES (?, ?, ?, ?)",
                                      (interaction.guild_id, user.id, interaction.user.id, text))
            await self.bot.track_staff(interaction.guild_id, interaction.user.id)
            return await interaction.response.send_message(embed=ok(f"Note added for {user}."), ephemeral=True)
        notes = await self.bot.db.fetchall("SELECT * FROM mod_notes WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15", (interaction.guild_id, user.id))
        embed = brand_embed(f"Notes for {user}", "\n".join(f"`{n['created_at'][:10]}` {n['note']} — <@{n['moderator_id']}>" for n in notes) or "No notes.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Moderation statistics for a moderator (or the whole team)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def modstats(self, interaction: discord.Interaction, moderator: discord.Member | None = None):
        gid = interaction.guild_id
        if moderator:
            rows = await self.bot.db.fetchall(
                """SELECT action, COUNT(*) AS n,
                          SUM(CASE WHEN created_at > datetime('now','-7 days') THEN 1 ELSE 0 END) AS week,
                          SUM(CASE WHEN created_at > datetime('now','-30 days') THEN 1 ELSE 0 END) AS month
                   FROM mod_cases WHERE guild_id=? AND moderator_id=? GROUP BY action""", (gid, moderator.id))
            embed = brand_embed(f"Mod stats · {moderator}")
            for r in rows:
                embed.add_field(name=r["action"].title(), value=f"7d: **{r['week']}** · 30d: **{r['month']}** · all: **{r['n']}**")
            if not rows:
                embed.description = "No moderation actions yet."
        else:
            rows = await self.bot.db.fetchall(
                """SELECT moderator_id, COUNT(*) AS n FROM mod_cases WHERE guild_id=? AND moderator_id IS NOT NULL
                   AND created_at > datetime('now','-30 days') GROUP BY moderator_id ORDER BY n DESC LIMIT 15""", (gid,))
            embed = brand_embed("Top moderators · last 30 days",
                                "\n".join(f"**{i + 1}.** <@{r['moderator_id']}> — {r['n']} actions" for i, r in enumerate(rows)) or "No actions yet.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------- appeals
    @app_commands.command(description="Appeal a punishment (works in DMs, even if you're banned)")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.describe(server_id="The server's ID (shown in your punishment DM)", case_number="Case number, if you know it")
    async def appeal(self, interaction: discord.Interaction, server_id: str, reason: app_commands.Range[str, 20, 1500], case_number: int | None = None):
        guild = self.bot.get_guild(int(server_id)) if server_id.isdigit() else None
        if not guild:
            return await interaction.response.send_message(embed=err("I'm not in a server with that ID."), ephemeral=True)
        s = await self.bot.settings.get(guild.id, "moderation")
        if not s["appeals_enabled"]:
            return await interaction.response.send_message(embed=err("That server doesn't accept appeals."), ephemeral=True)
        if case_number:
            case = await self.bot.db.fetchone("SELECT * FROM mod_cases WHERE guild_id=? AND case_number=? AND user_id=?", (guild.id, case_number, interaction.user.id))
        else:
            case = await self.bot.db.fetchone(
                "SELECT * FROM mod_cases WHERE guild_id=? AND user_id=? AND active=1 AND action IN ('ban','tempban','timeout','warn') ORDER BY id DESC LIMIT 1",
                (guild.id, interaction.user.id))
        if not case:
            return await interaction.response.send_message(embed=err("I couldn't find an active punishment for you in that server."), ephemeral=True)
        pending = await self.bot.db.fetchval("SELECT COUNT(*) FROM appeals WHERE guild_id=? AND user_id=? AND status='pending'", (guild.id, interaction.user.id), 0)
        if pending:
            return await interaction.response.send_message(embed=err("You already have a pending appeal there."), ephemeral=True)
        appeal_id = await self.bot.db.execute(
            "INSERT INTO appeals (guild_id, user_id, case_number, content) VALUES (?, ?, ?, ?)",
            (guild.id, interaction.user.id, case["case_number"], reason))
        channel = guild.get_channel(s["appeals_channel_id"]) if s["appeals_channel_id"] else None
        if isinstance(channel, discord.TextChannel):
            embed = brand_embed(f"Appeal #{appeal_id}", reason, WARNING)
            embed.add_field(name="User", value=f"{interaction.user.mention} ({interaction.user.id})")
            embed.add_field(name="Case", value=f"#{case['case_number']} · {case['action']}")
            embed.add_field(name="Original reason", value=case["reason"] or "—", inline=False)
            view = discord.ui.View(timeout=None)
            view.add_item(AppealButton("accept", appeal_id))
            view.add_item(AppealButton("deny", appeal_id))
            await channel.send(embed=embed, view=view)
        await interaction.response.send_message(embed=ok("Your appeal was submitted. Staff will review it — you'll get a DM with the decision."), ephemeral=True)

    # ------------------------------------------------------------ expiries
    @tasks.loop(seconds=60)
    async def expiry_loop(self):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM mod_cases WHERE action='tempban' AND active=1 AND expires_at IS NOT NULL AND expires_at <= ?", (to_db_time(utcnow()),))
        for c in rows:
            guild = self.bot.get_guild(c["guild_id"])
            await self.bot.db.execute("UPDATE mod_cases SET active=0 WHERE id=?", (c["id"],))
            if not guild:
                continue
            try:
                await guild.unban(discord.Object(c["user_id"]), reason=f"Temporary ban (case #{c['case_number']}) expired")
                await self.create_case(guild, "unban", c["user_id"], guild.me, f"Temporary ban #{c['case_number']} expired")
            except discord.HTTPException:
                pass
        # timeouts expire on Discord's side; mark them inactive for accurate stats
        await self.bot.db.execute("UPDATE mod_cases SET active=0 WHERE action='timeout' AND active=1 AND expires_at <= ?", (to_db_time(utcnow()),))

    @expiry_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Moderation(bot))
