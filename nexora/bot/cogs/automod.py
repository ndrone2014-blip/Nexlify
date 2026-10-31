"""Automod: content rules, spam, raids, suspicious accounts, and a moderator review queue."""
from __future__ import annotations

import re
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from nexora.bot.helpers import has_any_role, log_event
from nexora.core.automod_rules import RateTracker, RepeatTracker, Violation, check_message
from nexora.core.utils import DANGER, SUCCESS, WARNING, brand_embed, err, ok, truncate

RULE_NAMES = {
    "spam": "Spam / flood", "repeat": "Repeated messages", "invites": "Invite link", "links": "Link",
    "malicious": "Malicious link", "profanity": "Profanity", "mentions": "Mention spam", "emoji": "Emoji spam",
    "caps": "Excessive caps", "attachments": "Blocked attachment", "raid": "Raid", "newaccount": "Suspicious account",
}


class FlagButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:amflag:(?P<action>ok|fp):(?P<id>\d+)"):
    def __init__(self, action: str, flag_id: int):
        super().__init__(discord.ui.Button(
            label="Correct" if action == "ok" else "False positive (undo)",
            style=discord.ButtonStyle.secondary if action == "ok" else discord.ButtonStyle.danger,
            custom_id=f"nx:amflag:{action}:{flag_id}"))
        self.action, self.flag_id = action, flag_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_messages:  # type: ignore[union-attr]
            return await interaction.response.send_message(embed=err("You need Manage Messages to review automod."), ephemeral=True)
        cog: Automod = interaction.client.get_cog("Automod")  # type: ignore[assignment]
        result = await cog.review_flag(interaction.guild, self.flag_id, self.action == "ok", interaction.user)
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else brand_embed()
        embed.add_field(name="Review", value=f"{result} — {interaction.user.mention}", inline=False)
        embed.color = SUCCESS if self.action == "ok" else 0x64748B
        await interaction.response.edit_message(embed=embed, view=None)


class Automod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.rates = RateTracker()
        self.repeats = RepeatTracker()
        self.raid_until: dict[int, float] = {}
        self.locked_by_raid: dict[int, list[int]] = {}
        self._invite_cache: dict[int, tuple[float, set[str]]] = {}
        bot.add_dynamic_items(FlagButton)

    async def own_invites(self, guild: discord.Guild) -> set[str]:
        cached = self._invite_cache.get(guild.id)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        codes: set[str] = set()
        try:
            if guild.me.guild_permissions.manage_guild:
                codes = {i.code for i in await guild.invites()}
            if guild.vanity_url_code:
                codes.add(guild.vanity_url_code)
        except discord.HTTPException:
            pass
        self._invite_cache[guild.id] = (time.time(), codes)
        return codes

    async def exempt(self, member: discord.Member, channel_id: int, s: dict) -> bool:
        if member.bot or member.id == member.guild.owner_id:
            return True
        if s["exempt_staff"] and member.guild_permissions.manage_messages:
            return True
        if channel_id in s["exempt_channel_ids"]:
            return True
        return await has_any_role(member, s["exempt_role_ids"])

    # ------------------------------------------------------------- messages
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or not isinstance(message.author, discord.Member) or message.author.bot:
            return
        s = await self.bot.settings.get(message.guild.id, "automod")
        if not s["enabled"] or await self.exempt(message.author, message.channel.id, s):
            return
        key = (message.guild.id, message.author.id)
        spam_count = self.rates.hit(("msg",) + key, s["spam_seconds"]) if s["spam_enabled"] else None
        repeat_count = self.repeats.hit(key, message.content) if s["repeat_enabled"] else None
        own = await self.own_invites(message.guild) if s["invites_enabled"] and s["invites_allow_own"] and "/" in message.content else set()
        mentions = len(message.raw_mentions) + len(message.raw_role_mentions) + (5 if message.mention_everyone else 0)
        violation = check_message(s, message.content, mentions, [a.filename for a in message.attachments], own, spam_count, repeat_count)
        if violation:
            if violation.rule == "spam":
                self.rates.reset(("msg",) + key)
            await self.punish(message, violation, s)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content == after.content or not after.guild or not isinstance(after.author, discord.Member) or after.author.bot:
            return
        s = await self.bot.settings.get(after.guild.id, "automod")
        if not s["enabled"] or await self.exempt(after.author, after.channel.id, s):
            return
        own = await self.own_invites(after.guild) if s["invites_enabled"] and s["invites_allow_own"] else set()
        mentions = len(after.raw_mentions) + len(after.raw_role_mentions)
        violation = check_message(s, after.content, mentions, [], own, None, None)
        if violation:
            await self.punish(after, violation, s)

    async def punish(self, message: discord.Message, v: Violation, s: dict) -> None:
        guild, member = message.guild, message.author
        assert guild and isinstance(member, discord.Member)
        action = s.get(f"{v.rule}_action", "delete")
        taken: list[str] = []
        case_number = None
        if action in {"delete", "delete_warn", "delete_timeout"}:
            try:
                await message.delete()
                taken.append("deleted")
            except discord.HTTPException:
                taken.append("delete failed")
        reason = f"Automod: {RULE_NAMES.get(v.rule, v.rule)} ({v.detail})"
        mod = self.bot.get_cog("Moderation")
        if action in {"delete_warn", "warn"} and mod:
            case_number, count, esc = await mod.issue_warning(guild, member, guild.me, reason)
            taken.append(f"warned ({count})" + (f", {esc}" if esc else ""))
        if action in {"delete_timeout", "timeout"}:
            d = timedelta(minutes=s["timeout_minutes"])
            try:
                await member.timeout(d, reason=reason)
                if mod:
                    case_number = await mod.create_case(guild, "timeout", member, guild.me, reason, d)
                taken.append(f"timed out {s['timeout_minutes']}m")
            except discord.HTTPException:
                taken.append("timeout failed")
        if action == "flag":
            taken.append("flagged for review")
        await self.record_flag(guild, member, message.channel.id, v, message.content, ", ".join(taken), case_number,
                               pending=action == "flag")
        if "deleted" in taken and action != "flag":
            try:
                await message.channel.send(f"{member.mention}, your message was removed: **{RULE_NAMES.get(v.rule, v.rule)}**.", delete_after=6)
            except discord.HTTPException:
                pass

    async def record_flag(self, guild: discord.Guild, member: discord.abc.User, channel_id: int | None, v: Violation,
                          content: str | None, taken: str, case_number: int | None, pending: bool) -> None:
        flag_id = await self.bot.db.execute(
            """INSERT INTO automod_flags (guild_id, user_id, channel_id, rule, content, action_taken, case_number, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild.id, member.id, channel_id, v.rule, truncate(content, 2000), taken, case_number, "pending" if pending else "actioned"))
        await self.bot.track_feature(guild.id, f"automod:{v.rule}")
        s = await self.bot.settings.get(guild.id, "automod")
        embed = brand_embed(f"Automod · {RULE_NAMES.get(v.rule, v.rule)}", color=WARNING if pending else DANGER)
        embed.add_field(name="User", value=f"{member.mention} ({member.id})")
        if channel_id:
            embed.add_field(name="Channel", value=f"<#{channel_id}>")
        embed.add_field(name="Detail", value=v.detail)
        embed.add_field(name="Action", value=taken or "none")
        if content:
            embed.add_field(name="Content", value=truncate(content, 1000), inline=False)
        await log_event(self.bot, guild, "moderation", f"automod_{v.rule}", f"Automod {v.rule}: {member} — {v.detail} ({taken})",
                        None, user_id=member.id, channel_id=channel_id, data={"flag_id": flag_id, "detail": v.detail})
        channel = guild.get_channel(s["log_channel_id"]) if s["log_channel_id"] else None
        if isinstance(channel, discord.TextChannel):
            view = discord.ui.View(timeout=None)
            view.add_item(FlagButton("ok", flag_id))
            view.add_item(FlagButton("fp", flag_id))
            try:
                await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    async def review_flag(self, guild: discord.Guild, flag_id: int, correct: bool, reviewer: discord.abc.User) -> str:
        flag = await self.bot.db.fetchone("SELECT * FROM automod_flags WHERE id=? AND guild_id=?", (flag_id, guild.id))
        if not flag:
            return "Flag not found."
        if flag["status"] in {"confirmed", "dismissed"}:
            return f"Already {flag['status']}."
        notes = []
        if not correct:
            # False positive: undo what automod did.
            if flag["case_number"]:
                await self.bot.db.execute("UPDATE warnings SET active=0 WHERE guild_id=? AND case_number=?", (guild.id, flag["case_number"]))
                await self.bot.db.execute("UPDATE mod_cases SET active=0, reason = reason || ' [false positive]' WHERE guild_id=? AND case_number=?",
                                          (guild.id, flag["case_number"]))
                notes.append("warning/case voided")
            if "timed out" in (flag["action_taken"] or ""):
                m = guild.get_member(flag["user_id"])
                if m and m.is_timed_out():
                    try:
                        await m.timeout(None, reason="Automod false positive")
                        notes.append("timeout removed")
                    except discord.HTTPException:
                        pass
        await self.bot.db.execute("UPDATE automod_flags SET status=?, reviewer_id=? WHERE id=?",
                                  ("confirmed" if correct else "dismissed", reviewer.id, flag_id))
        await self.bot.track_staff(guild.id, reviewer.id)
        return ("Confirmed" if correct else "Marked false positive") + (f" ({', '.join(notes)})" if notes else "")

    # ---------------------------------------------------------- joins/raids
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = member.guild
        s = await self.bot.settings.get(g.id, "automod")
        if not s["enabled"] or member.bot:
            return
        if s["raid_enabled"]:
            count = self.rates.hit(("join", g.id), s["raid_seconds"])
            now = time.time()
            if count >= s["raid_joins"] and self.raid_until.get(g.id, 0) < now:
                self.raid_until[g.id] = now + 300
                await self.start_raid_mode(g, s, count)
            if self.raid_until.get(g.id, 0) > now and s["raid_action"] in {"timeout", "kick"}:
                try:
                    if s["raid_action"] == "kick":
                        await member.kick(reason="Automod: raid protection")
                    else:
                        await member.timeout(timedelta(hours=1), reason="Automod: raid protection")
                except discord.HTTPException:
                    pass
                await self.record_flag(g, member, None, Violation("raid", "Joined during a raid"), None, s["raid_action"], None, False)
                return
        if s["newaccount_enabled"]:
            age = discord.utils.utcnow() - member.created_at
            reasons = []
            if age < timedelta(days=s["newaccount_min_age_days"]):
                reasons.append(f"account is {age.days}d old")
            if s["newaccount_no_avatar"] and member.avatar is None:
                reasons.append("no avatar")
            if reasons:
                act = s["newaccount_action"]
                taken = "flagged for review"
                try:
                    if act == "kick":
                        await member.kick(reason="Automod: suspicious account")
                        taken = "kicked"
                    elif act == "timeout":
                        await member.timeout(timedelta(hours=12), reason="Automod: suspicious account")
                        taken = "timed out 12h"
                except discord.HTTPException:
                    taken = f"{act} failed"
                await self.record_flag(g, member, None, Violation("newaccount", ", ".join(reasons)), None, taken, None, act == "flag")

    async def start_raid_mode(self, guild: discord.Guild, s: dict, count: int) -> None:
        locked = []
        if s["raid_action"] == "lockdown":
            for ch in guild.text_channels:
                ow = ch.overwrites_for(guild.default_role)
                if ow.send_messages is False:
                    continue
                ow.send_messages = False
                try:
                    await ch.set_permissions(guild.default_role, overwrite=ow, reason="Automod: raid lockdown")
                    locked.append(ch.id)
                except discord.HTTPException:
                    pass
            self.locked_by_raid[guild.id] = locked
        embed = brand_embed("🚨 Raid detected", f"**{count}** joins in {s['raid_seconds']}s. Raid mode is active for 5 minutes.\n"
                            f"Action: **{s['raid_action']}**" + (f"\nLocked {len(locked)} channels. Use `/raid end` to unlock." if locked else ""), DANGER)
        await log_event(self.bot, guild, "moderation", "raid_detected", f"Raid detected: {count} joins", embed, data={"locked": locked})
        ch = guild.get_channel(s["log_channel_id"]) if s["log_channel_id"] else None
        if isinstance(ch, discord.TextChannel):
            await ch.send(embed=embed)
        self.bot.trigger(guild, "raid_detected", joins=count)

    raid = app_commands.Group(name="raid", description="Raid protection controls", guild_only=True,
                              default_permissions=discord.Permissions(manage_guild=True))

    @raid.command(name="end", description="End raid mode and unlock channels locked by automod")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def raid_end(self, interaction: discord.Interaction):
        self.raid_until.pop(interaction.guild_id, None)
        unlocked = 0
        for cid in self.locked_by_raid.pop(interaction.guild_id, []):
            ch = interaction.guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel):
                ow = ch.overwrites_for(interaction.guild.default_role)
                ow.send_messages = None
                try:
                    await ch.set_permissions(interaction.guild.default_role, overwrite=None if ow.is_empty() else ow, reason="Raid ended")
                    unlocked += 1
                except discord.HTTPException:
                    pass
        await interaction.response.send_message(embed=ok(f"Raid mode ended. Unlocked {unlocked} channels."))


async def setup(bot):
    await bot.add_cog(Automod(bot))
