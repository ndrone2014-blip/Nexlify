"""Automation builder runtime: triggers → conditions → multiple actions, with execution history."""
from __future__ import annotations

import contextvars
import time
from datetime import timedelta

import discord
from discord.ext import commands

from nexora.bot.helpers import log_event
from nexora.core.automation_engine import check_conditions
from nexora.core.utils import brand_embed, color_int, render, truncate
from nexora.db import jload

# Prevents automations from triggering each other in an infinite loop.
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("nexora_automation_depth", default=0)
MAX_DEPTH = 2


class Automation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._has: dict[tuple[int, str], tuple[float, bool]] = {}

    def invalidate(self, guild_id: int) -> None:
        """Called by the dashboard after automations change."""
        for k in [k for k in self._has if k[0] == guild_id]:
            self._has.pop(k, None)

    async def _guild_has(self, guild_id: int, trigger: str) -> bool:
        cached = self._has.get((guild_id, trigger))
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        n = await self.bot.db.fetchval("SELECT COUNT(*) FROM automations WHERE guild_id=? AND trigger=? AND enabled=1", (guild_id, trigger), 0)
        self._has[(guild_id, trigger)] = (time.monotonic(), n > 0)
        return n > 0

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild and not message.author.bot:
            self.bot.trigger(message.guild, "message_sent", member=message.author, channel=message.channel, content=message.content)

    @commands.Cog.listener()
    async def on_nexora_trigger(self, guild: discord.Guild, name: str, context: dict):
        if _depth.get() >= MAX_DEPTH or not await self._guild_has(guild.id, name):
            return
        autos = await self.bot.db.fetchall("SELECT * FROM automations WHERE guild_id=? AND trigger=? AND enabled=1", (guild.id, name))
        if not autos:
            return
        ctx = await self.build_context(guild, context)
        token = _depth.set(_depth.get() + 1)
        try:
            for a in autos:
                await self.run(guild, a, context, ctx)
        finally:
            _depth.reset(token)

    async def build_context(self, guild: discord.Guild, context: dict) -> dict:
        member = context.get("member")
        ctx: dict = {
            "content": context.get("content"),
            "channel_id": getattr(context.get("channel"), "id", None),
            "role_id": getattr(context.get("role"), "id", None),
            "level": context.get("level"),
        }
        if isinstance(member, discord.Member):
            ctx["role_ids"] = {r.id for r in member.roles}
            ctx["is_bot"] = member.bot
            ctx["account_age_days"] = (discord.utils.utcnow() - member.created_at).days
            if ctx["level"] is None:
                ctx["level"] = await self.bot.db.fetchval("SELECT level FROM levels WHERE guild_id=? AND user_id=?", (guild.id, member.id), 0)
            ctx["warnings"] = context.get("warnings") or await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=? AND active=1", (guild.id, member.id), 0)
        return ctx

    def _text(self, template: str, guild: discord.Guild, context: dict) -> str:
        extra = {k: v for k, v in context.items() if isinstance(v, (str, int, float))}
        if context.get("role") is not None:
            extra["role"] = context["role"].mention
            extra["role.name"] = context["role"].name
        if context.get("channel") is not None:
            extra["channel"] = getattr(context["channel"], "mention", "")
        return render(template, context.get("member"), guild, **extra)

    async def run(self, guild: discord.Guild, auto: dict, context: dict, ctx: dict) -> None:
        passed, why = check_conditions(jload(auto["conditions"], []), ctx)
        if not passed:
            # Skips for high-volume triggers would flood the history, so only record them for rarer triggers.
            if auto["trigger"] != "message_sent":
                await self._record(auto, "skipped", why)
            return
        errors, done = [], []
        member = context.get("member") if isinstance(context.get("member"), discord.Member) else None
        for i, act in enumerate(jload(auto["actions"], [])):
            try:
                await self.execute(guild, act, context, member)
                done.append(act["type"])
            except Exception as exc:  # report and continue with other actions
                errors.append(f"#{i + 1} {act['type']}: {getattr(exc, 'text', None) or exc}")
        await self.bot.db.execute("UPDATE automations SET runs = runs + 1, last_run_at = datetime('now') WHERE id=?", (auto["id"],))
        await self._record(auto, "error" if errors else "ok", "; ".join(errors) if errors else f"ran: {', '.join(done)}")

    async def _record(self, auto: dict, status: str, detail: str) -> None:
        await self.bot.db.execute("INSERT INTO automation_runs (automation_id, guild_id, status, detail) VALUES (?, ?, ?, ?)",
                                  (auto["id"], auto["guild_id"], status, truncate(detail, 1000)))

    async def execute(self, guild: discord.Guild, act: dict, context: dict, member: discord.Member | None) -> None:
        t = act["type"]
        if t in {"send_message", "send_embed", "notify_staff"}:
            ch = guild.get_channel(act["channel"])
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                raise ValueError("channel not found")
            text = self._text(act.get("content", ""), guild, context)
            if t == "send_embed":
                await ch.send(embed=brand_embed(self._text(act.get("title", ""), guild, context) or None, text or None, color_int(act.get("color"))))
            elif t == "notify_staff":
                general = await self.bot.settings.get(guild.id, "general")
                pings = " ".join(f"<@&{r}>" for r in general["staff_role_ids"])
                await ch.send(f"{pings} {text}".strip(), allowed_mentions=discord.AllowedMentions(roles=True, users=True))
            else:
                await ch.send(text, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        elif t == "dm_member":
            if not member:
                raise ValueError("this trigger has no member")
            await member.send(self._text(act["content"], guild, context))
        elif t in {"add_role", "remove_role"}:
            role = guild.get_role(act["role"])
            if not member or not role:
                raise ValueError("member or role not found")
            if t == "add_role":
                await member.add_roles(role, reason="Nexora automation")
            else:
                await member.remove_roles(role, reason="Nexora automation")
        elif t == "create_log":
            await log_event(self.bot, guild, "server", "automation", self._text(act["content"], guild, context),
                            brand_embed("Automation", self._text(act["content"], guild, context)), user_id=member.id if member else None)
        elif t == "create_ticket":
            panel = await self.bot.db.fetchone("SELECT * FROM ticket_panels WHERE id=? AND guild_id=?", (act["panel"], guild.id))
            tickets = self.bot.get_cog("Tickets")
            if not member or not panel or not tickets:
                raise ValueError("member or ticket panel not found")
            await tickets.create_ticket(member, panel, [])
        elif t == "add_xp":
            lv = self.bot.get_cog("Leveling")
            if not member or not lv:
                raise ValueError("no member")
            await lv.add_xp(member, act["amount"], None)
        elif t == "give_currency":
            eco = self.bot.get_cog("Economy")
            if not member or not eco:
                raise ValueError("no member")
            await eco.add_balance(guild.id, member.id, act["amount"])
        elif t == "timeout_member":
            if not member:
                raise ValueError("no member")
            await member.timeout(timedelta(minutes=max(1, min(40320, act["amount"]))), reason="Nexora automation")
        else:
            raise ValueError(f"unknown action {t}")


async def setup(bot):
    await bot.add_cog(Automation(bot))
