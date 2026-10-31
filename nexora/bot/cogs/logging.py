"""Advanced logging.

Every event is stored in `log_events` (searchable on the dashboard) and, when a
channel is configured, posted as an embed. Discord only exposes some details
(e.g. who deleted a message, who kicked someone) via the audit log, and only if
the bot has View Audit Log; when that information isn't available the log says
so instead of guessing.
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import discord
from discord.ext import commands

from nexora.core.utils import DANGER, INFO, SUCCESS, WARNING, brand_embed, truncate
from nexora.db import jdump

COLORS = {"create": SUCCESS, "delete": DANGER, "update": WARNING, "info": INFO}


def _perm_diff(before: discord.Permissions, after: discord.Permissions) -> tuple[list[str], list[str]]:
    added, removed = [], []
    for name, value in after:
        old = getattr(before, name)
        if value and not old:
            added.append(name)
        elif old and not value:
            removed.append(name)
    return added, removed


def _fmt_perms(names: list[str]) -> str:
    return ", ".join(n.replace("_", " ").title() for n in names) or "—"


class Logging(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._voice_started: dict[tuple[int, int], float] = {}

    # ------------------------------------------------------------- core API
    async def record(
        self,
        guild: discord.Guild,
        category: str,
        event_type: str,
        summary: str,
        embed: discord.Embed | None = None,
        *,
        user_id: int | None = None,
        moderator_id: int | None = None,
        channel_id: int | None = None,
        data: dict | None = None,
        files: list[discord.File] | None = None,
    ) -> None:
        s = await self.bot.settings.get(guild.id, "logging")
        if not s["enabled"] or not s.get(f"{category}_enabled", True):
            return
        if channel_id and channel_id in s["ignored_channel_ids"]:
            return
        if user_id and s["ignored_role_ids"]:
            member = guild.get_member(user_id)
            if member and any(r.id in s["ignored_role_ids"] for r in member.roles):
                return
        await self.bot.db.execute(
            """INSERT INTO log_events (guild_id, category, event_type, user_id, moderator_id, channel_id, summary, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild.id, category, event_type, user_id, moderator_id, channel_id, truncate(summary, 1000), jdump(data or {})),
        )
        target_id = s.get(f"{category}_channel_id") or s["default_channel_id"]
        if not target_id or embed is None:
            return
        channel = guild.get_channel(target_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        embed.timestamp = discord.utils.utcnow()
        if not embed.footer.text:
            embed.set_footer(text=f"{category} · {event_type}" + (f" · user {user_id}" if user_id else ""))
        try:
            await channel.send(embed=embed, files=files or [], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def audit_lookup(
        self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int | None = None, within: float = 15.0
    ) -> discord.AuditLogEntry | None:
        """Find the audit-log entry for an action that just happened, if Discord exposes one."""
        me = guild.me
        if not me or not me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(1.0)  # audit log entries land slightly after gateway events
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                if (discord.utils.utcnow() - entry.created_at).total_seconds() > within:
                    break
                if target_id is None or getattr(entry.target, "id", None) == target_id:
                    return entry
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    @staticmethod
    def _who(entry: discord.AuditLogEntry | None) -> str:
        if entry is None:
            return "*Unknown (not in audit log or missing View Audit Log)*"
        reason = f"\nReason: {entry.reason}" if entry.reason else ""
        return f"{entry.user.mention if entry.user else 'Unknown'}{reason}"

    def _by_me(self, entry: discord.AuditLogEntry | None) -> bool:
        """Actions Nexora performed itself already have a detailed case log."""
        return bool(entry and entry.user and self.bot.user and entry.user.id == self.bot.user.id)

    async def _ignored_bot(self, guild: discord.Guild, author: discord.abc.User | None) -> bool:
        if author is None or not author.bot:
            return False
        s = await self.bot.settings.get(guild.id, "logging")
        return s["ignore_bots"]

    # -------------------------------------------------------------- messages
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild and not message.author.bot:
            await self.bot.bump_stat(message.guild.id, "messages")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not guild:
            return
        msg = payload.cached_message
        if msg and await self._ignored_bot(guild, msg.author):
            return
        embed = brand_embed(color=COLORS["delete"])
        if msg:
            embed.set_author(name=f"{msg.author} ({msg.author.id})", icon_url=msg.author.display_avatar.url)
            embed.title = "Message deleted"
            embed.description = truncate(msg.content, 3900) or "*No text content*"
            embed.add_field(name="Channel", value=f"<#{payload.channel_id}>")
            embed.add_field(name="Sent", value=discord.utils.format_dt(msg.created_at, "R"))
            if msg.attachments:
                embed.add_field(
                    name=f"Attachments ({len(msg.attachments)})",
                    value=truncate("\n".join(f"{a.filename} ({a.size // 1024} KB)" for a in msg.attachments), 1024)
                    + "\n*Discord removes deleted attachments from its CDN, so only names are kept.*",
                    inline=False,
                )
            if msg.embeds:
                embed.add_field(name="Embeds", value=f"{len(msg.embeds)} embed(s): " + truncate(", ".join(e.title or "untitled" for e in msg.embeds), 900), inline=False)
            if msg.reference and msg.reference.message_id:
                embed.add_field(name="Reply to", value=f"[jump](https://discord.com/channels/{guild.id}/{payload.channel_id}/{msg.reference.message_id})")
            deleter = None
            entry = await self.audit_lookup(guild, discord.AuditLogAction.message_delete, msg.author.id, within=30)
            if entry and getattr(entry.extra, "channel", None) and entry.extra.channel.id == payload.channel_id:
                deleter = entry.user
            embed.add_field(
                name="Deleted by",
                value=deleter.mention if deleter else "Author or unknown (Discord only logs deletions by others, and batches them)",
                inline=False,
            )
            data = {
                "content": msg.content,
                "author": str(msg.author),
                "attachments": [{"filename": a.filename, "url": a.url, "size": a.size} for a in msg.attachments],
                "embeds": [e.to_dict() for e in msg.embeds][:5],
                "deleted_by": deleter.id if deleter else None,
                "message_id": payload.message_id,
            }
            await self.record(guild, "messages", "message_delete", f"Message by {msg.author} deleted in #{getattr(msg.channel, 'name', '?')}: {truncate(msg.content, 200)}",
                              embed, user_id=msg.author.id, moderator_id=deleter.id if deleter else None, channel_id=payload.channel_id, data=data)
        else:
            embed.title = "Message deleted (not cached)"
            embed.description = (
                f"A message in <#{payload.channel_id}> was deleted.\nIt was sent before I started or is too old to be cached, so its content is unavailable."
            )
            await self.record(guild, "messages", "message_delete", f"Uncached message {payload.message_id} deleted",
                              embed, channel_id=payload.channel_id, data={"message_id": payload.message_id, "cached": False})

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not guild:
            return
        cached = sorted(payload.cached_messages, key=lambda m: m.created_at)
        lines = [f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author} ({m.author.id}): {m.content}" for m in cached]
        embed = brand_embed("Bulk message delete", f"**{len(payload.message_ids)}** messages deleted in <#{payload.channel_id}>.\n"
                            f"{len(cached)} were cached and are attached.", COLORS["delete"])
        files = []
        if lines:
            import io
            files.append(discord.File(io.BytesIO("\n".join(lines).encode()), filename="deleted-messages.txt"))
        await self.record(guild, "messages", "bulk_delete", f"{len(payload.message_ids)} messages bulk deleted",
                          embed, channel_id=payload.channel_id, data={"count": len(payload.message_ids), "messages": lines[:500]}, files=files)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not guild:
            return
        after = payload.message
        before = payload.cached_message
        if after.author and await self._ignored_bot(guild, after.author):
            return
        if before is not None and before.content == after.content:
            return  # embed unfurl / pin, not a real edit
        if after.content is None or (after.author and after.author.bot and before is None):
            return
        embed = brand_embed("Message edited", color=COLORS["update"])
        if after.author:
            embed.set_author(name=f"{after.author} ({after.author.id})", icon_url=after.author.display_avatar.url)
        embed.add_field(name="Before", value=truncate(before.content, 1024) if before else "*Not cached — original content unavailable*", inline=False)
        embed.add_field(name="After", value=truncate(after.content, 1024) or "*empty*", inline=False)
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}> · [Jump to message]({after.jump_url})")
        await self.record(
            guild, "messages", "message_edit",
            f"{after.author} edited a message in #{getattr(after.channel, 'name', '?')}",
            embed, user_id=after.author.id if after.author else None, channel_id=payload.channel_id,
            data={"before": before.content if before else None, "after": after.content, "url": after.jump_url},
        )

    # --------------------------------------------------------------- members
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.bot.bump_stat(member.guild.id, "joins")
        age = discord.utils.utcnow() - member.created_at
        embed = brand_embed("Member joined", f"{member.mention} {member}", COLORS["create"])
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Account created", value=f"{discord.utils.format_dt(member.created_at, 'F')} ({discord.utils.format_dt(member.created_at, 'R')})")
        embed.add_field(name="Member #", value=str(member.guild.member_count))
        if age < timedelta(days=7):
            embed.add_field(name="⚠️ New account", value=f"Created {age.days}d {age.seconds // 3600}h ago", inline=False)
        if member.bot:
            entry = await self.audit_lookup(member.guild, discord.AuditLogAction.bot_add, member.id)
            embed.add_field(name="Added by", value=self._who(entry), inline=False)
        await self.record(member.guild, "members", "member_join", f"{member} joined", embed, user_id=member.id,
                          data={"account_age_days": age.days, "bot": member.bot})

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self.bot.bump_stat(member.guild.id, "leaves")
        roles = [r.mention for r in member.roles if not r.is_default()]
        entry = await self.audit_lookup(member.guild, discord.AuditLogAction.kick, member.id)
        if entry and not self._by_me(entry):
            embed = brand_embed("Member kicked", f"{member.mention} {member}", COLORS["delete"])
            embed.add_field(name="Moderator", value=self._who(entry), inline=False)
            await self.record(member.guild, "moderation", "kick", f"{member} was kicked by {entry.user}", embed,
                              user_id=member.id, moderator_id=entry.user.id if entry.user else None, data={"reason": entry.reason})
            await self.bot.track_staff(member.guild.id, entry.user.id if entry.user else None)
        embed = brand_embed("Member left", f"{member.mention} {member}", COLORS["delete"])
        embed.set_thumbnail(url=member.display_avatar.url)
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"))
        embed.add_field(name=f"Roles ({len(roles)})", value=truncate(" ".join(roles), 1024) or "None", inline=False)
        await self.record(member.guild, "members", "member_leave", f"{member} left", embed, user_id=member.id,
                          data={"roles": [r.id for r in member.roles if not r.is_default()], "kicked": bool(entry)})

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        g = after.guild
        if before.nick != after.nick:
            entry = await self.audit_lookup(g, discord.AuditLogAction.member_update, after.id)
            embed = brand_embed("Nickname changed", after.mention, COLORS["update"])
            embed.add_field(name="Before", value=before.nick or "*none*")
            embed.add_field(name="After", value=after.nick or "*none*")
            if entry and entry.user and entry.user.id != after.id:
                embed.add_field(name="Changed by", value=entry.user.mention)
            await self.record(g, "members", "nickname_change", f"{after} nickname: {before.nick} → {after.nick}", embed,
                              user_id=after.id, moderator_id=entry.user.id if entry and entry.user else None,
                              data={"before": before.nick, "after": after.nick})
        if before.roles != after.roles:
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            entry = await self.audit_lookup(g, discord.AuditLogAction.member_role_update, after.id)
            embed = brand_embed("Roles updated", after.mention, COLORS["update"])
            if added:
                embed.add_field(name="Added", value=" ".join(r.mention for r in added))
            if removed:
                embed.add_field(name="Removed", value=" ".join(r.mention for r in removed))
            embed.add_field(name="By", value=self._who(entry), inline=False)
            await self.record(g, "roles", "member_roles", f"{after}: +{[r.name for r in added]} -{[r.name for r in removed]}", embed,
                              user_id=after.id, moderator_id=entry.user.id if entry and entry.user else None,
                              data={"added": [r.id for r in added], "removed": [r.id for r in removed]})
            for r in added:
                self.bot.trigger(g, "role_added", member=after, role=r)
            for r in removed:
                self.bot.trigger(g, "role_removed", member=after, role=r)
        if before.timed_out_until != after.timed_out_until:
            entry = await self.audit_lookup(g, discord.AuditLogAction.member_update, after.id)
        if before.timed_out_until != after.timed_out_until and not self._by_me(entry):
            if after.timed_out_until and after.timed_out_until > discord.utils.utcnow():
                embed = brand_embed("Member timed out", after.mention, COLORS["delete"])
                embed.add_field(name="Until", value=discord.utils.format_dt(after.timed_out_until, "F"))
                etype = "timeout"
            else:
                embed = brand_embed("Timeout removed", after.mention, COLORS["create"])
                etype = "untimeout"
            embed.add_field(name="Moderator", value=self._who(entry), inline=False)
            await self.record(g, "moderation", etype, f"{after} {etype}", embed, user_id=after.id,
                              moderator_id=entry.user.id if entry and entry.user else None,
                              data={"until": after.timed_out_until.isoformat() if after.timed_out_until else None})
        if before.premium_since is None and after.premium_since is not None:
            embed = brand_embed("Server boosted! 🚀", f"{after.mention} boosted the server.", 0xF47FFF)
            await self.record(g, "members", "boost", f"{after} boosted", embed, user_id=after.id)
        elif before.premium_since is not None and after.premium_since is None:
            embed = brand_embed("Boost ended", f"{after.mention} is no longer boosting.", COLORS["delete"])
            await self.record(g, "members", "unboost", f"{after} stopped boosting", embed, user_id=after.id)
        if before.pending and not after.pending:
            embed = brand_embed("Membership screening passed", after.mention, COLORS["create"])
            await self.record(g, "members", "screening_passed", f"{after} passed membership screening", embed, user_id=after.id)
        if before.guild_avatar != after.guild_avatar:
            embed = brand_embed("Server avatar changed", after.mention, COLORS["update"])
            if after.guild_avatar:
                embed.set_thumbnail(url=after.guild_avatar.url)
            await self.record(g, "members", "server_avatar", f"{after} changed server avatar", embed, user_id=after.id)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        changes = []
        if before.name != after.name:
            changes.append(("Username", before.name, after.name, "username_change"))
        if before.global_name != after.global_name:
            changes.append(("Display name", before.global_name or "—", after.global_name or "—", "display_name_change"))
        avatar_changed = before.avatar != after.avatar
        if not changes and not avatar_changed:
            return
        for guild in self.bot.guilds:
            if not guild.get_member(after.id):
                continue
            for label, old, new, etype in changes:
                embed = brand_embed(f"{label} changed", after.mention, COLORS["update"])
                embed.add_field(name="Before", value=old)
                embed.add_field(name="After", value=new)
                await self.record(guild, "members", etype, f"{label}: {old} → {new}", embed, user_id=after.id, data={"before": old, "after": new})
            if avatar_changed:
                embed = brand_embed("Avatar changed", after.mention, COLORS["update"])
                embed.set_thumbnail(url=after.display_avatar.url)
                await self.record(guild, "members", "avatar_change", f"{after} changed avatar", embed, user_id=after.id,
                                  data={"after": after.display_avatar.url})

    # ------------------------------------------------------------ moderation
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        entry = await self.audit_lookup(guild, discord.AuditLogAction.ban, user.id)
        if self._by_me(entry):
            return
        embed = brand_embed("Member banned", f"{user.mention} {user} ({user.id})", COLORS["delete"])
        embed.add_field(name="Moderator", value=self._who(entry), inline=False)
        await self.record(guild, "moderation", "ban", f"{user} banned", embed, user_id=user.id,
                          moderator_id=entry.user.id if entry and entry.user else None, data={"reason": entry.reason if entry else None})

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        entry = await self.audit_lookup(guild, discord.AuditLogAction.unban, user.id)
        if self._by_me(entry):
            return
        embed = brand_embed("Member unbanned", f"{user.mention} {user} ({user.id})", COLORS["create"])
        embed.add_field(name="Moderator", value=self._who(entry), inline=False)
        await self.record(guild, "moderation", "unban", f"{user} unbanned", embed, user_id=user.id,
                          moderator_id=entry.user.id if entry and entry.user else None)

    # ----------------------------------------------------------------- roles
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        entry = await self.audit_lookup(role.guild, discord.AuditLogAction.role_create, role.id)
        embed = brand_embed("Role created", f"{role.mention} `{role.name}`", COLORS["create"])
        embed.add_field(name="By", value=self._who(entry), inline=False)
        if role.permissions.administrator:
            embed.add_field(name="⚠️ Administrator", value="This role has Administrator.", inline=False)
        await self.record(role.guild, "roles", "role_create", f"Role {role.name} created", embed,
                          moderator_id=entry.user.id if entry and entry.user else None, data={"role_id": role.id})

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        entry = await self.audit_lookup(role.guild, discord.AuditLogAction.role_delete, role.id)
        embed = brand_embed("Role deleted", f"`{role.name}` ({role.id}) · {len(role.members)} members had it", COLORS["delete"])
        embed.add_field(name="By", value=self._who(entry), inline=False)
        await self.record(role.guild, "roles", "role_delete", f"Role {role.name} deleted", embed,
                          moderator_id=entry.user.id if entry and entry.user else None, data={"role_id": role.id, "name": role.name})

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes: dict[str, tuple[str, str]] = {}
        if before.name != after.name:
            changes["Name"] = (before.name, after.name)
        if before.color != after.color:
            changes["Color"] = (str(before.color), str(after.color))
        if before.hoist != after.hoist:
            changes["Displayed separately"] = (str(before.hoist), str(after.hoist))
        if before.mentionable != after.mentionable:
            changes["Mentionable"] = (str(before.mentionable), str(after.mentionable))
        if before.position != after.position:
            changes["Position"] = (str(before.position), str(after.position))
        added, removed = _perm_diff(before.permissions, after.permissions)
        if not changes and not added and not removed:
            return
        # Position shifts cascade to many roles at once; log them without audit lookups to avoid spam.
        only_position = list(changes) == ["Position"] and not added and not removed
        entry = None if only_position else await self.audit_lookup(after.guild, discord.AuditLogAction.role_update, after.id)
        embed = brand_embed("Role updated", after.mention, COLORS["update"])
        for k, (a, b) in changes.items():
            embed.add_field(name=k, value=f"{a} → {b}", inline=False)
        if added:
            embed.add_field(name="Permissions granted", value=_fmt_perms(added), inline=False)
        if removed:
            embed.add_field(name="Permissions removed", value=_fmt_perms(removed), inline=False)
        if "administrator" in added:
            embed.add_field(name="⚠️ Administrator granted", value="This role now has full Administrator access.", inline=False)
            embed.color = DANGER
        if not only_position:
            embed.add_field(name="By", value=self._who(entry), inline=False)
        await self.record(after.guild, "roles", "role_update", f"Role {after.name} updated", embed,
                          moderator_id=entry.user.id if entry and entry.user else None,
                          data={"changes": changes, "perms_added": added, "perms_removed": removed, "role_id": after.id})

    # -------------------------------------------------------------- channels
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        entry = await self.audit_lookup(channel.guild, discord.AuditLogAction.channel_create, channel.id)
        embed = brand_embed("Channel created", f"{channel.mention} `{channel.name}` ({channel.type})", COLORS["create"])
        if channel.category:
            embed.add_field(name="Category", value=channel.category.name)
        embed.add_field(name="By", value=self._who(entry), inline=False)
        await self.record(channel.guild, "channels", "channel_create", f"Channel #{channel.name} created", embed,
                          moderator_id=entry.user.id if entry and entry.user else None, channel_id=channel.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        entry = await self.audit_lookup(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
        embed = brand_embed("Channel deleted", f"`#{channel.name}` ({channel.type}, {channel.id})", COLORS["delete"])
        embed.add_field(name="By", value=self._who(entry), inline=False)
        await self.record(channel.guild, "channels", "channel_delete", f"Channel #{channel.name} deleted", embed,
                          moderator_id=entry.user.id if entry and entry.user else None, data={"channel_id": channel.id, "name": channel.name})

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        changes: dict[str, tuple[str, str]] = {}
        if before.name != after.name:
            changes["Name"] = (before.name, after.name)
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes["Topic"] = (truncate(getattr(before, "topic", None) or "—", 400), truncate(getattr(after, "topic", None) or "—", 400))
        if before.category_id != after.category_id:
            changes["Category"] = (before.category.name if before.category else "—", after.category.name if after.category else "—")
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes["Slowmode"] = (f"{getattr(before, 'slowmode_delay', 0)}s", f"{getattr(after, 'slowmode_delay', 0)}s")
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            changes["NSFW"] = (str(getattr(before, "nsfw", None)), str(getattr(after, "nsfw", None)))
        overwrite_lines = []
        if before.overwrites != after.overwrites:
            targets = set(before.overwrites) | set(after.overwrites)
            for t in targets:
                b = before.overwrites.get(t, discord.PermissionOverwrite())
                a = after.overwrites.get(t, discord.PermissionOverwrite())
                if b == a:
                    continue
                diffs = []
                for (name, av) in a:
                    bv = dict(iter(b)).get(name)
                    if av != bv:
                        sym = {True: "✅", False: "❌", None: "⬜"}
                        diffs.append(f"{name.replace('_', ' ')}: {sym[bv]}→{sym[av]}")
                tname = t.mention if hasattr(t, "mention") else str(t)
                overwrite_lines.append(f"**{tname}**: " + ", ".join(diffs[:8]))
        if not changes and not overwrite_lines:
            return
        action = discord.AuditLogAction.overwrite_update if overwrite_lines and not changes else discord.AuditLogAction.channel_update
        entry = await self.audit_lookup(after.guild, action, after.id)
        embed = brand_embed("Channel updated", after.mention, COLORS["update"])
        for k, (a, b) in changes.items():
            embed.add_field(name=k, value=f"{a} → {b}", inline=False)
        if overwrite_lines:
            embed.add_field(name="Permission overwrites", value=truncate("\n".join(overwrite_lines), 1024), inline=False)
        embed.add_field(name="By", value=self._who(entry), inline=False)
        await self.record(after.guild, "channels", "channel_update", f"Channel #{after.name} updated", embed,
                          moderator_id=entry.user.id if entry and entry.user else None, channel_id=after.id,
                          data={"changes": changes, "overwrites": overwrite_lines})

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        embed = brand_embed("Thread created", f"{thread.mention} in <#{thread.parent_id}> by <@{thread.owner_id}>", COLORS["create"])
        await self.record(thread.guild, "channels", "thread_create", f"Thread {thread.name} created", embed,
                          user_id=thread.owner_id, channel_id=thread.id)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        embed = brand_embed("Thread deleted", f"`{thread.name}` in <#{thread.parent_id}>", COLORS["delete"])
        await self.record(thread.guild, "channels", "thread_delete", f"Thread {thread.name} deleted", embed, channel_id=thread.id)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: {before.name} → {after.name}")
        if before.archived != after.archived:
            changes.append("Archived" if after.archived else "Unarchived")
        if before.locked != after.locked:
            changes.append("Locked" if after.locked else "Unlocked")
        if not changes:
            return
        embed = brand_embed("Thread updated", f"{after.mention}\n" + "\n".join(changes), COLORS["update"])
        await self.record(after.guild, "channels", "thread_update", f"Thread {after.name}: {', '.join(changes)}", embed, channel_id=after.id)

    # ----------------------------------------------------------------- voice
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        g = member.guild
        key = (g.id, member.id)
        events: list[tuple[str, str, int]] = []
        if before.channel is None and after.channel is not None:
            self._voice_started[key] = time.time()
            events.append(("voice_join", f"joined {after.channel.mention}", SUCCESS))
        elif before.channel is not None and after.channel is None:
            started = self._voice_started.pop(key, None)
            if started:
                await self.bot.bump_stat(g.id, "voice_minutes", int((time.time() - started) // 60))
            events.append(("voice_leave", f"left {before.channel.mention}", DANGER))
        elif before.channel and after.channel and before.channel.id != after.channel.id:
            events.append(("voice_move", f"moved {before.channel.mention} → {after.channel.mention}", WARNING))
        if before.mute != after.mute:
            events.append(("server_mute", "was server " + ("muted" if after.mute else "unmuted"), WARNING))
        if before.deaf != after.deaf:
            events.append(("server_deafen", "was server " + ("deafened" if after.deaf else "undeafened"), WARNING))
        if before.self_stream != after.self_stream:
            events.append(("stream", ("started" if after.self_stream else "stopped") + " streaming", INFO))
        if before.self_video != after.self_video:
            events.append(("video", ("turned on" if after.self_video else "turned off") + " their camera", INFO))
        if before.suppress != after.suppress and after.channel and isinstance(after.channel, discord.StageChannel):
            events.append(("stage", "became a stage " + ("audience member" if after.suppress else "speaker"), INFO))
        if before.requested_to_speak_at is None and after.requested_to_speak_at is not None:
            events.append(("stage_request", "requested to speak on stage", INFO))
        for etype, text, color in events:
            mod_id = None
            if etype in {"server_mute", "server_deafen"}:
                entry = await self.audit_lookup(g, discord.AuditLogAction.member_update, member.id)
                if entry and entry.user:
                    mod_id = entry.user.id
                    text += f" by {entry.user.mention}"
            embed = brand_embed("Voice activity", f"{member.mention} {text}", color)
            await self.record(g, "voice", etype, f"{member} {discord.utils.remove_markdown(text)}", embed, user_id=member.id,
                              moderator_id=mod_id, channel_id=(after.channel or before.channel).id if (after.channel or before.channel) else None)

    # ---------------------------------------------------------------- server
    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        changes: dict[str, tuple[str, str]] = {}
        for attr, label in [
            ("name", "Name"), ("description", "Description"), ("verification_level", "Verification level"),
            ("explicit_content_filter", "Content filter"), ("default_notifications", "Default notifications"),
            ("afk_timeout", "AFK timeout"), ("afk_channel", "AFK channel"), ("system_channel", "System channel"),
            ("rules_channel", "Rules channel"), ("mfa_level", "2FA requirement"), ("vanity_url_code", "Vanity URL"),
            ("owner_id", "Owner"), ("preferred_locale", "Locale"), ("premium_tier", "Boost tier"),
        ]:
            b, a = getattr(before, attr, None), getattr(after, attr, None)
            if b != a:
                changes[label] = (str(b), str(a))
        if before.icon != after.icon:
            changes["Icon"] = ("old", "new")
        if before.banner != after.banner:
            changes["Banner"] = ("old", "new")
        if not changes:
            return
        entry = await self.audit_lookup(after, discord.AuditLogAction.guild_update)
        embed = brand_embed("Server updated", color=COLORS["update"])
        for k, (a, b) in changes.items():
            embed.add_field(name=k, value=f"{truncate(a, 200)} → {truncate(b, 200)}", inline=False)
        if before.icon != after.icon and after.icon:
            embed.set_thumbnail(url=after.icon.url)
        embed.add_field(name="By", value=self._who(entry), inline=False)
        await self.record(after, "server", "guild_update", "Server settings changed: " + ", ".join(changes), embed,
                          moderator_id=entry.user.id if entry and entry.user else None, data={"changes": changes})

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if not invite.guild or not isinstance(invite.guild, discord.Guild):
            return
        embed = brand_embed("Invite created", f"`{invite.code}` for <#{invite.channel.id if invite.channel else 0}>", COLORS["create"])
        embed.add_field(name="By", value=invite.inviter.mention if invite.inviter else "Unknown")
        embed.add_field(name="Max uses", value=str(invite.max_uses or "∞"))
        embed.add_field(name="Expires", value=discord.utils.format_dt(invite.expires_at, "R") if invite.expires_at else "Never")
        await self.record(invite.guild, "server", "invite_create", f"Invite {invite.code} created by {invite.inviter}", embed,
                          user_id=invite.inviter.id if invite.inviter else None, data={"code": invite.code})

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if not invite.guild or not isinstance(invite.guild, discord.Guild):
            return
        embed = brand_embed("Invite deleted", f"`{invite.code}`", COLORS["delete"])
        await self.record(invite.guild, "server", "invite_delete", f"Invite {invite.code} deleted", embed, data={"code": invite.code})

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        entry = None
        for action in (discord.AuditLogAction.webhook_create, discord.AuditLogAction.webhook_update, discord.AuditLogAction.webhook_delete):
            entry = await self.audit_lookup(channel.guild, action)
            if entry:
                break
        what = entry.action.name.replace("_", " ") if entry else "webhook changed"
        embed = brand_embed("Webhooks updated", f"{what.title()} in {channel.mention}", COLORS["update"])
        embed.add_field(name="By", value=self._who(entry))
        await self.record(channel.guild, "server", "webhooks_update", f"Webhooks updated in #{channel.name}", embed,
                          moderator_id=entry.user.id if entry and entry.user else None, channel_id=channel.id)

    @commands.Cog.listener()
    async def on_guild_integrations_update(self, guild: discord.Guild):
        embed = brand_embed("Integrations updated", "An integration (bot, app or connection) was added, removed or changed.", COLORS["update"])
        await self.record(guild, "server", "integrations_update", "Integrations updated", embed)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        added = [e for e in after if e not in before]
        removed = [e for e in before if e not in after]
        if not added and not removed:
            return
        desc = ""
        if added:
            desc += "Added: " + " ".join(str(e) for e in added) + "\n"
        if removed:
            desc += "Removed: " + ", ".join(f"`{e.name}`" for e in removed)
        await self.record(guild, "server", "emojis_update", desc.strip(), brand_embed("Emojis updated", desc, COLORS["update"]))


async def setup(bot):
    await bot.add_cog(Logging(bot))
