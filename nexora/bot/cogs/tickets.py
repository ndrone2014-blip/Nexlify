"""Ticket system: panels, question modals, claiming, transfers, transcripts, ratings, priorities."""
from __future__ import annotations

import html
import io
import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from nexora.bot.helpers import has_any_role, log_event
from nexora.core.utils import BRAND, DANGER, SUCCESS, WARNING, brand_embed, color_int, err, ok, render, truncate
from nexora.db import jdump, jload

PRIORITY_EMOJI = {"low": "🟢", "normal": "🔵", "high": "🟠", "urgent": "🔴"}


# ------------------------------------------------------------------ UI items
class OpenTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:tkopen:(?P<panel>\d+)"):
    def __init__(self, panel_id: int, label: str = "Open Ticket", emoji: str | None = None):
        super().__init__(discord.ui.Button(label=label, emoji=emoji or "🎫", style=discord.ButtonStyle.primary, custom_id=f"nx:tkopen:{panel_id}"))
        self.panel_id = panel_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["panel"]))

    async def callback(self, interaction: discord.Interaction):
        cog: Tickets = interaction.client.get_cog("Tickets")  # type: ignore[assignment]
        await cog.start_open(interaction, self.panel_id)


class TicketControl(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:tk:(?P<action>claim|close|closereason|reopen|delete):(?P<id>\d+)"):
    STYLES = {
        "claim": ("Claim", "🙋", discord.ButtonStyle.success),
        "close": ("Close", "🔒", discord.ButtonStyle.danger),
        "closereason": ("Close with reason", "📝", discord.ButtonStyle.secondary),
        "reopen": ("Reopen", "🔓", discord.ButtonStyle.success),
        "delete": ("Delete", "🗑️", discord.ButtonStyle.danger),
    }

    def __init__(self, action: str, ticket_id: int):
        label, emoji, style = self.STYLES[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"nx:tk:{action}:{ticket_id}"))
        self.action, self.ticket_id = action, ticket_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        cog: Tickets = interaction.client.get_cog("Tickets")  # type: ignore[assignment]
        ticket = await cog.get_ticket(self.ticket_id)
        if not ticket:
            return await interaction.response.send_message(embed=err("Ticket not found."), ephemeral=True)
        is_staff = await cog.is_staff(interaction.user, ticket)  # type: ignore[arg-type]
        if self.action == "claim":
            if not is_staff:
                return await interaction.response.send_message(embed=err("Only staff can claim tickets."), ephemeral=True)
            await cog.claim(ticket, interaction.user)
            return await interaction.response.send_message(embed=ok(f"{interaction.user.mention} claimed this ticket."))
        if self.action == "close":
            if not is_staff and interaction.user.id != ticket["opener_id"]:
                return await interaction.response.send_message(embed=err("You can't close this ticket."), ephemeral=True)
            await interaction.response.send_message(embed=brand_embed("Closing ticket…", color=WARNING))
            return await cog.close(ticket, interaction.user, None)
        if self.action == "closereason":
            if not is_staff and interaction.user.id != ticket["opener_id"]:
                return await interaction.response.send_message(embed=err("You can't close this ticket."), ephemeral=True)
            return await interaction.response.send_modal(CloseReasonModal(cog, ticket))
        if not is_staff:
            return await interaction.response.send_message(embed=err("Only staff can do that."), ephemeral=True)
        if self.action == "reopen":
            await interaction.response.defer()
            msg = await cog.reopen(ticket, interaction.user)
            return await interaction.followup.send(embed=ok(msg))
        if self.action == "delete":
            await interaction.response.send_message(embed=brand_embed("Deleting channel in 5 seconds…", color=DANGER))
            await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=5))
            if isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")


class RateButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:tkrate:(?P<id>\d+):(?P<n>[1-5])"):
    def __init__(self, ticket_id: int, n: int):
        super().__init__(discord.ui.Button(label="⭐" * n, style=discord.ButtonStyle.secondary, custom_id=f"nx:tkrate:{ticket_id}:{n}"))
        self.ticket_id, self.n = ticket_id, n

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]), int(match["n"]))

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db  # type: ignore[attr-defined]
        t = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not t or t["opener_id"] != interaction.user.id:
            return await interaction.response.send_message("You can't rate this ticket.", ephemeral=True)
        await db.execute("UPDATE tickets SET rating=? WHERE id=?", (self.n, self.ticket_id))
        await interaction.response.edit_message(content=f"Thanks! You rated ticket #{t['number']} {'⭐' * self.n}", view=None, embed=None)


class QuestionsModal(discord.ui.Modal):
    def __init__(self, cog: "Tickets", panel: dict):
        super().__init__(title=truncate(panel["title"] or "Open a ticket", 45), timeout=600)
        self.cog, self.panel = cog, panel
        self.inputs: list[discord.ui.TextInput] = []
        for q in jload(panel["questions"], [])[:5]:  # Discord modals hold at most 5 inputs
            ti = discord.ui.TextInput(
                label=truncate(q.get("label") or "Question", 45),
                placeholder=truncate(q.get("placeholder") or "", 100) or None,
                required=bool(q.get("required", True)),
                style=discord.TextStyle.paragraph if q.get("long") else discord.TextStyle.short,
                max_length=1000 if q.get("long") else 200,
            )
            self.add_item(ti)
            self.inputs.append(ti)

    async def on_submit(self, interaction: discord.Interaction):
        answers = [{"question": ti.label, "answer": ti.value} for ti in self.inputs]
        await self.cog.open_ticket(interaction, self.panel, answers)


class CloseReasonModal(discord.ui.Modal, title="Close ticket"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, cog: "Tickets", ticket: dict):
        super().__init__()
        self.cog, self.ticket = cog, ticket

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=brand_embed("Closing ticket…", f"Reason: {self.reason.value}", WARNING))
        await self.cog.close(self.ticket, interaction.user, self.reason.value)


def control_view(ticket_id: int, closed: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    actions = ["reopen", "delete"] if closed else ["claim", "close", "closereason"]
    for a in actions:
        view.add_item(TicketControl(a, ticket_id))
    return view


# ---------------------------------------------------------------- transcript
def build_transcript(guild_name: str, channel_name: str, ticket: dict, messages: list[discord.Message]) -> str:
    rows = []
    for m in messages:
        content = html.escape(m.content or "")
        for e in m.embeds:
            content += f"<div class='embed'><b>{html.escape(e.title or '')}</b><br>{html.escape(e.description or '')}</div>"
        for a in m.attachments:
            content += f"<div class='att'>📎 <a href='{html.escape(a.url)}'>{html.escape(a.filename)}</a></div>"
        rows.append(
            f"<div class='msg'><img src='{html.escape(m.author.display_avatar.url)}'><div><span class='a'>{html.escape(str(m.author))}</span>"
            f"<span class='t'>{m.created_at:%Y-%m-%d %H:%M} UTC</span><div class='c'>{content}</div></div></div>"
        )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Ticket #{ticket['number']} transcript</title>
<style>body{{background:#0f0b1a;color:#e9e4f5;font-family:system-ui,sans-serif;margin:0;padding:24px}}
h1{{color:#a78bfa;font-size:20px}}.msg{{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid #221a38}}
.msg img{{width:36px;height:36px;border-radius:50%}}.a{{font-weight:600;color:#fff;margin-right:8px}}.t{{color:#8b80a8;font-size:12px}}
.c{{white-space:pre-wrap;margin-top:2px}}.embed{{border-left:3px solid #7c3aed;background:#1a1330;padding:6px 10px;margin-top:4px;border-radius:4px}}
.att a{{color:#a78bfa}}</style></head><body><h1>{html.escape(guild_name)} · #{html.escape(channel_name)} · Ticket #{ticket['number']}</h1>
<p>Opened by user {ticket['opener_id']} · {len(messages)} messages</p>{''.join(rows)}</body></html>"""


# ---------------------------------------------------------------------- cog
class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_dynamic_items(OpenTicketButton, TicketControl, RateButton)
        self.auto_close.start()

    def cog_unload(self):
        self.auto_close.cancel()

    async def get_ticket(self, ticket_id: int) -> dict | None:
        return await self.bot.db.fetchone("SELECT * FROM tickets WHERE id=?", (ticket_id,))

    async def ticket_for_channel(self, channel_id: int) -> dict | None:
        return await self.bot.db.fetchone("SELECT * FROM tickets WHERE channel_id=? ORDER BY id DESC LIMIT 1", (channel_id,))

    async def staff_roles(self, ticket_or_panel: dict) -> list[int]:
        panel = ticket_or_panel
        if "panel_id" in ticket_or_panel:
            panel = await self.bot.db.fetchone("SELECT * FROM ticket_panels WHERE id=?", (ticket_or_panel["panel_id"],)) or {}
        roles = jload(panel.get("staff_role_ids"), []) if panel else []
        if not roles and panel:
            general = await self.bot.settings.get(panel["guild_id"], "general")
            roles = general["staff_role_ids"]
        return roles

    async def is_staff(self, member: discord.Member, ticket: dict) -> bool:
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            return True
        return await has_any_role(member, await self.staff_roles(ticket))

    # ------------------------------------------------------------ panels
    async def publish_panel(self, panel_id: int) -> discord.Message:
        """Post (or re-post) a panel message. Used by the dashboard and /ticket panel."""
        panel = await self.bot.db.fetchone("SELECT * FROM ticket_panels WHERE id=?", (panel_id,))
        if not panel:
            raise ValueError("Panel not found")
        guild = self.bot.get_guild(panel["guild_id"])
        channel = guild.get_channel(panel["channel_id"]) if guild and panel["channel_id"] else None
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Panel channel not set or not found")
        embed = brand_embed(panel["title"], panel["description"], color_int(panel["color"]) if panel["color"] else BRAND)
        view = discord.ui.View(timeout=None)
        view.add_item(OpenTicketButton(panel_id, panel["button_label"], panel["button_emoji"]))
        if panel["message_id"]:
            try:
                old = await channel.fetch_message(panel["message_id"])
                await old.edit(embed=embed, view=view)
                return old
            except discord.HTTPException:
                pass
        msg = await channel.send(embed=embed, view=view)
        await self.bot.db.execute("UPDATE ticket_panels SET message_id=? WHERE id=?", (msg.id, panel_id))
        return msg

    async def start_open(self, interaction: discord.Interaction, panel_id: int):
        panel = await self.bot.db.fetchone("SELECT * FROM ticket_panels WHERE id=? AND guild_id=?", (panel_id, interaction.guild_id))
        s = await self.bot.settings.get(interaction.guild_id, "tickets")
        if not panel or not panel["enabled"] or not s["enabled"]:
            return await interaction.response.send_message(embed=err("Tickets are currently disabled."), ephemeral=True)
        open_count = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM tickets WHERE guild_id=? AND opener_id=? AND panel_id=? AND status='open'",
            (interaction.guild_id, interaction.user.id, panel_id), 0)
        if panel["max_open_per_user"] and open_count >= panel["max_open_per_user"]:
            existing = await self.bot.db.fetchone("SELECT channel_id FROM tickets WHERE guild_id=? AND opener_id=? AND panel_id=? AND status='open'",
                                                  (interaction.guild_id, interaction.user.id, panel_id))
            return await interaction.response.send_message(embed=err(f"You already have an open ticket: <#{existing['channel_id']}>"), ephemeral=True)
        if jload(panel["questions"], []):
            return await interaction.response.send_modal(QuestionsModal(self, panel))
        await self.open_ticket(interaction, panel, [])

    async def open_ticket(self, interaction: discord.Interaction, panel: dict, answers: list[dict]):
        member = interaction.user
        assert interaction.guild and isinstance(member, discord.Member)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await self.create_ticket(member, panel, answers)
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=err(f"I couldn't create the channel: {exc.text or 'missing permissions'}"), ephemeral=True)
        await interaction.followup.send(embed=ok(f"Your ticket was created: {channel.mention}"), ephemeral=True)

    async def create_ticket(self, member: discord.Member, panel: dict, answers: list[dict]) -> discord.TextChannel:
        """Create a ticket channel for `member`. Used by the panel button and by automations."""
        guild = member.guild
        s = await self.bot.settings.get(guild.id, "tickets")
        number = await self.bot.db.next_number("tickets", "number", guild.id)
        staff = [guild.get_role(r) for r in await self.staff_roles(panel)]
        staff = [r for r in staff if r]
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True, embed_links=True),
        }
        for r in staff:
            overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
        category = guild.get_channel(panel["category_id"]) if panel["category_id"] else None
        name = render(panel["name_format"] or "ticket-{number}", member, guild, number=f"{number:04d}", username=member.name)
        name = re.sub(r"[^a-z0-9\-_]", "", name.lower().replace(" ", "-"))[:90] or f"ticket-{number}"
        channel = await guild.create_text_channel(
            name, overwrites=overwrites, category=category if isinstance(category, discord.CategoryChannel) else None,
            topic=f"Ticket #{number} · opened by {member} ({member.id})", reason=f"Ticket opened by {member}")
        ticket_id = await self.bot.db.execute(
            "INSERT INTO tickets (guild_id, panel_id, number, channel_id, opener_id, answers) VALUES (?, ?, ?, ?, ?, ?)",
            (guild.id, panel["id"], number, channel.id, member.id, jdump(answers)))
        embed = brand_embed(f"Ticket #{number} · {panel['name']}", render(panel["welcome_message"], member, guild), color_int(panel["color"]) if panel["color"] else BRAND)
        for a in answers:
            embed.add_field(name=truncate(a["question"], 256), value=truncate(a["answer"], 1024) or "—", inline=False)
        ping = member.mention + (" " + " ".join(r.mention for r in staff) if s["ping_staff"] and staff else "")
        await channel.send(content=ping, embed=embed, view=control_view(ticket_id),
                           allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await log_event(self.bot, guild, "server", "ticket_open", f"Ticket #{number} opened by {member}",
                        brand_embed("Ticket opened", f"#{number} {channel.mention} by {member.mention} ({panel['name']})", SUCCESS),
                        user_id=member.id, channel_id=channel.id, data={"ticket_id": ticket_id})
        await self.bot.track_feature(guild.id, "tickets:open")
        self.bot.trigger(guild, "ticket_opened", member=member, channel=channel, ticket_number=number, panel=panel["name"])
        return channel

    # ------------------------------------------------------------ actions
    async def claim(self, ticket: dict, staff: discord.abc.User) -> None:
        await self.bot.db.execute("UPDATE tickets SET claimed_by=? WHERE id=?", (staff.id, ticket["id"]))
        await self.bot.track_staff(ticket["guild_id"], staff.id)

    async def close(self, ticket: dict, closer: discord.abc.User | None, reason: str | None) -> str:
        ticket = await self.get_ticket(ticket["id"]) or ticket
        if ticket["status"] == "closed":
            return "Ticket is already closed."
        guild = self.bot.get_guild(ticket["guild_id"])
        if not guild:
            return "Server unavailable."
        s = await self.bot.settings.get(guild.id, "tickets")
        channel = guild.get_channel(ticket["channel_id"])
        transcript = None
        if isinstance(channel, discord.TextChannel):
            messages = [m async for m in channel.history(limit=2000, oldest_first=True)]
            transcript = build_transcript(guild.name, channel.name, ticket, messages)
        await self.bot.db.execute(
            "UPDATE tickets SET status='closed', closed_at=datetime('now'), closed_by=?, close_reason=?, transcript=COALESCE(?, transcript) WHERE id=?",
            (closer.id if closer else None, reason, transcript, ticket["id"]))
        if closer:
            await self.bot.track_staff(guild.id, closer.id)
        opener = guild.get_member(ticket["opener_id"]) or self.bot.get_user(ticket["opener_id"])
        summary = brand_embed(f"Ticket #{ticket['number']} closed", color=DANGER)
        summary.add_field(name="Opened by", value=f"<@{ticket['opener_id']}>")
        summary.add_field(name="Closed by", value=closer.mention if closer else "Automatic")
        summary.add_field(name="Claimed by", value=f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "—")
        summary.add_field(name="Reason", value=reason or "—", inline=False)
        if transcript and s["transcript_channel_id"]:
            tch = guild.get_channel(s["transcript_channel_id"])
            if isinstance(tch, discord.TextChannel):
                try:
                    await tch.send(embed=summary, file=discord.File(io.BytesIO(transcript.encode()), filename=f"ticket-{ticket['number']}.html"))
                except discord.HTTPException:
                    pass
        if opener and (s["dm_transcript"] or s["ratings_enabled"]):
            try:
                files = [discord.File(io.BytesIO(transcript.encode()), filename=f"ticket-{ticket['number']}.html")] if transcript and s["dm_transcript"] else []
                view = None
                if s["ratings_enabled"]:
                    view = discord.ui.View(timeout=None)
                    for n in range(1, 6):
                        view.add_item(RateButton(ticket["id"], n))
                dm = brand_embed(f"Your ticket in {guild.name} was closed", f"Reason: {reason or '—'}" + ("\n\nHow did we do? Rate your experience below." if view else ""))
                await opener.send(embed=dm, files=files, view=view)
            except discord.HTTPException:
                pass
        if isinstance(channel, discord.TextChannel):
            try:
                if isinstance(opener, discord.Member):
                    await channel.set_permissions(opener, view_channel=True, send_messages=False, read_message_history=True)
                if s["delete_after_close_seconds"]:
                    await channel.send(embed=brand_embed("Ticket closed", f"This channel will be deleted in {s['delete_after_close_seconds']} seconds.", DANGER))
                    await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=s["delete_after_close_seconds"]))
                    await channel.delete(reason=f"Ticket #{ticket['number']} closed")
                else:
                    await channel.edit(name=f"closed-{ticket['number']:04d}")
                    await channel.send(embed=brand_embed("Ticket closed", "Staff can reopen or delete this ticket.", DANGER), view=control_view(ticket["id"], closed=True))
            except discord.HTTPException:
                pass
        await log_event(self.bot, guild, "server", "ticket_close", f"Ticket #{ticket['number']} closed by {closer or 'system'}",
                        summary, user_id=ticket["opener_id"], moderator_id=closer.id if closer else None, data={"ticket_id": ticket["id"], "reason": reason})
        self.bot.trigger(guild, "ticket_closed", member=opener, ticket_number=ticket["number"], reason=reason or "")
        return "Ticket closed."

    async def reopen(self, ticket: dict, by: discord.abc.User) -> str:
        guild = self.bot.get_guild(ticket["guild_id"])
        channel = guild.get_channel(ticket["channel_id"]) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return "The ticket channel was deleted, so it can't be reopened. The transcript is still on the dashboard."
        opener = guild.get_member(ticket["opener_id"])
        if opener:
            await channel.set_permissions(opener, view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
        await channel.edit(name=f"ticket-{ticket['number']:04d}")
        await self.bot.db.execute("UPDATE tickets SET status='open', closed_at=NULL WHERE id=?", (ticket["id"],))
        await self.bot.track_staff(guild.id, by.id)
        await channel.send(embed=brand_embed("Ticket reopened", f"Reopened by {by.mention}", SUCCESS), view=control_view(ticket["id"]))
        return "Ticket reopened."

    # ------------------------------------------------------------ commands
    ticket = app_commands.Group(name="ticket", description="Ticket commands", guild_only=True)

    async def _current(self, interaction: discord.Interaction, staff_only: bool = True) -> dict | None:
        t = await self.ticket_for_channel(interaction.channel_id)
        if not t:
            await interaction.response.send_message(embed=err("This isn't a ticket channel."), ephemeral=True)
            return None
        if staff_only and not await self.is_staff(interaction.user, t):  # type: ignore[arg-type]
            await interaction.response.send_message(embed=err("Only staff can do that."), ephemeral=True)
            return None
        return t

    @ticket.command(name="panel", description="Post a ticket panel created on the dashboard")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel_cmd(self, interaction: discord.Interaction, panel_id: int):
        try:
            msg = await self.publish_panel(panel_id)
        except ValueError as exc:
            return await interaction.response.send_message(embed=err(f"{exc}. Create and configure panels on the dashboard."), ephemeral=True)
        await interaction.response.send_message(embed=ok(f"Panel posted: {msg.jump_url}"), ephemeral=True)

    @panel_cmd.autocomplete("panel_id")
    async def _panel_ac(self, interaction: discord.Interaction, current: str):
        rows = await self.bot.db.fetchall("SELECT id, name FROM ticket_panels WHERE guild_id=? ORDER BY id LIMIT 25", (interaction.guild_id,))
        return [app_commands.Choice(name=f"{r['name']} (#{r['id']})", value=r["id"]) for r in rows if current.lower() in r["name"].lower()]

    @ticket.command(name="close", description="Close this ticket")
    async def close_cmd(self, interaction: discord.Interaction, reason: str | None = None):
        t = await self._current(interaction, staff_only=False)
        if not t:
            return
        if not await self.is_staff(interaction.user, t) and interaction.user.id != t["opener_id"]:  # type: ignore[arg-type]
            return await interaction.response.send_message(embed=err("You can't close this ticket."), ephemeral=True)
        await interaction.response.send_message(embed=brand_embed("Closing ticket…", reason, WARNING))
        await self.close(t, interaction.user, reason)

    @ticket.command(name="claim", description="Claim this ticket")
    async def claim_cmd(self, interaction: discord.Interaction):
        t = await self._current(interaction)
        if t:
            await self.claim(t, interaction.user)
            await interaction.response.send_message(embed=ok(f"{interaction.user.mention} claimed this ticket."))

    @ticket.command(name="transfer", description="Transfer this ticket to another staff member")
    async def transfer_cmd(self, interaction: discord.Interaction, member: discord.Member):
        t = await self._current(interaction)
        if not t:
            return
        if not await self.is_staff(member, t):
            return await interaction.response.send_message(embed=err("That member isn't ticket staff."), ephemeral=True)
        await self.claim(t, member)
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
        await interaction.response.send_message(embed=ok(f"Ticket transferred to {member.mention}."))

    @ticket.command(name="add", description="Add a member to this ticket")
    async def add_cmd(self, interaction: discord.Interaction, member: discord.Member):
        if await self._current(interaction):
            await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
            await interaction.response.send_message(embed=ok(f"Added {member.mention}."))

    @ticket.command(name="remove", description="Remove a member from this ticket")
    async def remove_cmd(self, interaction: discord.Interaction, member: discord.Member):
        if await self._current(interaction):
            await interaction.channel.set_permissions(member, overwrite=None)
            await interaction.response.send_message(embed=ok(f"Removed {member.mention}."))

    @ticket.command(name="priority", description="Set this ticket's priority")
    @app_commands.choices(level=[app_commands.Choice(name=k.title(), value=k) for k in PRIORITY_EMOJI])
    async def priority_cmd(self, interaction: discord.Interaction, level: app_commands.Choice[str]):
        t = await self._current(interaction)
        if not t:
            return
        await self.bot.db.execute("UPDATE tickets SET priority=? WHERE id=?", (level.value, t["id"]))
        base = re.sub(r"^[🟢🔵🟠🔴]-?", "", interaction.channel.name)
        await interaction.response.send_message(embed=ok(f"Priority set to {PRIORITY_EMOJI[level.value]} **{level.name}**."))
        try:
            await interaction.channel.edit(name=f"{PRIORITY_EMOJI[level.value]}-{base}" if level.value != "normal" else base)
        except discord.HTTPException:
            pass  # channel renames are rate limited to 2 per 10 minutes

    @ticket.command(name="rename", description="Rename this ticket channel")
    async def rename_cmd(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 90]):
        if await self._current(interaction):
            await interaction.channel.edit(name=name)
            await interaction.response.send_message(embed=ok(f"Renamed to **{name}**."))

    @ticket.command(name="reopen", description="Reopen this closed ticket")
    async def reopen_cmd(self, interaction: discord.Interaction):
        t = await self._current(interaction)
        if t:
            await interaction.response.defer()
            await interaction.followup.send(embed=ok(await self.reopen(t, interaction.user)))

    # ------------------------------------------------------------ auto-close
    @tasks.loop(minutes=30)
    async def auto_close(self):
        rows = await self.bot.db.fetchall("SELECT * FROM tickets WHERE status='open'")
        for t in rows:
            s = await self.bot.settings.get(t["guild_id"], "tickets")
            if not s["auto_close_hours"]:
                continue
            guild = self.bot.get_guild(t["guild_id"])
            ch = guild.get_channel(t["channel_id"]) if guild else None
            if not isinstance(ch, discord.TextChannel):
                continue
            last = discord.utils.snowflake_time(ch.last_message_id) if ch.last_message_id else ch.created_at
            if discord.utils.utcnow() - last > timedelta(hours=s["auto_close_hours"]):
                await self.close(t, None, f"Inactive for {s['auto_close_hours']} hours")

    @auto_close.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Tickets(bot))
