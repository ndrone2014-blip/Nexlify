"""Giveaways: button entry, required/bonus roles, account-age & level requirements, scheduling,
rerolls, extensions, cancellation and verified winners."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from nexora.bot.helpers import has_any_role, log_event
from nexora.core.giveaway_logic import entries_for, pick_winners
from nexora.core.utils import BRAND, DANGER, SUCCESS, brand_embed, err, human_duration, ok, parse_duration, truncate
from nexora.db import from_db_time, jdump, jload, to_db_time, utcnow


class EnterButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:gw:(?P<id>\d+)"):
    def __init__(self, gid: int, label: str = "Enter", count: int | None = None):
        text = f"{label} ({count})" if count is not None else label
        super().__init__(discord.ui.Button(label=text, emoji="🎉", style=discord.ButtonStyle.primary, custom_id=f"nx:gw:{gid}"))
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Giveaways").toggle_entry(interaction, self.gid)  # type: ignore[union-attr]


class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_dynamic_items(EnterButton)
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    # ------------------------------------------------------------ rendering
    async def build_embed(self, g: dict) -> discord.Embed:
        ends = from_db_time(g["ends_at"])
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id=?", (g["id"],), 0)
        status = g["status"]
        color = {"running": BRAND, "ended": 0x64748B, "cancelled": DANGER, "scheduled": BRAND}[status]
        embed = brand_embed(f"🎉 {truncate(g['prize'], 240)}", g["description"] or None, color)
        if status == "running":
            embed.add_field(name="Ends", value=f"{discord.utils.format_dt(ends, 'R')} ({discord.utils.format_dt(ends, 'f')})")
        elif status == "ended":
            winners = jload(g["winners"], [])
            embed.add_field(name="Winners", value=", ".join(f"<@{w}>" for w in winners) or "No valid entries", inline=False)
            embed.add_field(name="Ended", value=discord.utils.format_dt(ends, "f"))
        elif status == "cancelled":
            embed.add_field(name="Status", value="Cancelled")
        embed.add_field(name="Winners", value=str(g["winner_count"]))
        embed.add_field(name="Entries", value=str(count))
        embed.add_field(name="Hosted by", value=f"<@{g['host_id']}>" if g["host_id"] else "Nexora")
        reqs = []
        req_roles = jload(g["required_role_ids"], [])
        if req_roles:
            reqs.append("Role: " + " or ".join(f"<@&{r}>" for r in req_roles))
        if g["min_account_age_days"]:
            reqs.append(f"Account at least {g['min_account_age_days']} days old")
        if g["min_level"]:
            reqs.append(f"Level {g['min_level']}+")
        if reqs:
            embed.add_field(name="Requirements", value="\n".join(reqs), inline=False)
        bonus = jload(g["bonus_roles"], {})
        if bonus:
            embed.add_field(name="Bonus entries", value="\n".join(f"<@&{r}>: +{n}" for r, n in bonus.items()), inline=False)
        embed.set_footer(text=f"Giveaway #{g['id']}")
        return embed

    async def view_for(self, g: dict) -> discord.ui.View | None:
        if g["status"] != "running":
            return None
        s = await self.bot.settings.get(g["guild_id"], "giveaways")
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id=?", (g["id"],), 0)
        view = discord.ui.View(timeout=None)
        view.add_item(EnterButton(g["id"], s["button_label"], count))
        return view

    async def refresh_message(self, g: dict) -> None:
        guild = self.bot.get_guild(g["guild_id"])
        ch = guild.get_channel(g["channel_id"]) if guild else None
        if not isinstance(ch, discord.TextChannel) or not g["message_id"]:
            return
        try:
            msg = ch.get_partial_message(g["message_id"])
            await msg.edit(embed=await self.build_embed(g), view=await self.view_for(g))
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------- services
    async def create(self, guild: discord.Guild, channel_id: int, host_id: int | None, prize: str, duration: timedelta,
                     winners: int = 1, description: str = "", required_role_ids: list[int] | None = None,
                     bonus_roles: dict | None = None, min_account_age_days: int = 0, min_level: int = 0,
                     starts_at: datetime | None = None) -> int:
        start = starts_at or utcnow()
        status = "scheduled" if starts_at and starts_at > utcnow() + timedelta(seconds=30) else "running"
        gid = await self.bot.db.execute(
            """INSERT INTO giveaways (guild_id, channel_id, host_id, prize, description, winner_count, starts_at, ends_at,
               required_role_ids, bonus_roles, min_account_age_days, min_level, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (guild.id, channel_id, host_id, prize, description, winners, to_db_time(start), to_db_time(start + duration),
             jdump(required_role_ids or []), jdump({str(k): int(v) for k, v in (bonus_roles or {}).items()}),
             min_account_age_days, min_level, status))
        if status == "running":
            await self.post(gid)
        await self.bot.track_feature(guild.id, "giveaways:create")
        return gid

    async def post(self, gid: int) -> None:
        g = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
        guild = self.bot.get_guild(g["guild_id"])
        ch = guild.get_channel(g["channel_id"]) if guild else None
        if not isinstance(ch, discord.TextChannel):
            await self.bot.db.execute("UPDATE giveaways SET status='cancelled' WHERE id=?", (gid,))
            return
        g["status"] = "running"
        s = await self.bot.settings.get(guild.id, "giveaways")
        ping = f"<@&{s['ping_role_id']}>" if s["ping_role_id"] else None
        msg = await ch.send(content=ping, embed=await self.build_embed(g), view=await self.view_for(g),
                            allowed_mentions=discord.AllowedMentions(roles=True))
        await self.bot.db.execute("UPDATE giveaways SET message_id=?, status='running' WHERE id=?", (msg.id, gid))

    async def check_requirements(self, member: discord.Member, g: dict) -> str | None:
        req = jload(g["required_role_ids"], [])
        if req and not await has_any_role(member, req):
            return "You don't have a required role for this giveaway."
        if g["min_account_age_days"] and (discord.utils.utcnow() - member.created_at).days < g["min_account_age_days"]:
            return f"Your account must be at least {g['min_account_age_days']} days old."
        if g["min_level"]:
            lvl = await self.bot.db.fetchval("SELECT level FROM levels WHERE guild_id=? AND user_id=?", (member.guild.id, member.id), 0)
            if lvl < g["min_level"]:
                return f"You need to be level {g['min_level']} (you're level {lvl})."
        return None

    async def toggle_entry(self, interaction: discord.Interaction, gid: int):
        g = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=? AND guild_id=?", (gid, interaction.guild_id))
        if not g or g["status"] != "running":
            return await interaction.response.send_message(embed=err("This giveaway isn't running."), ephemeral=True)
        member = interaction.user
        assert isinstance(member, discord.Member)
        existing = await self.bot.db.fetchone("SELECT 1 FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", (gid, member.id))
        if existing:
            await self.bot.db.execute("DELETE FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", (gid, member.id))
            await interaction.response.send_message(embed=ok("You left the giveaway."), ephemeral=True)
        else:
            problem = await self.check_requirements(member, g)
            if problem:
                return await interaction.response.send_message(embed=err(problem), ephemeral=True)
            n = entries_for({r.id for r in member.roles}, jload(g["bonus_roles"], {}))
            await self.bot.db.execute("INSERT INTO giveaway_entries (giveaway_id, user_id, entries) VALUES (?, ?, ?)", (gid, member.id, n))
            await interaction.response.send_message(embed=ok(f"You're in! 🎉 ({n} entr{'y' if n == 1 else 'ies'})"), ephemeral=True)
        await self.refresh_message(g)

    async def draw(self, g: dict, count: int, exclude: set[int]) -> list[int]:
        guild = self.bot.get_guild(g["guild_id"])
        rows = await self.bot.db.fetchall("SELECT user_id, entries FROM giveaway_entries WHERE giveaway_id=?", (g["id"],))
        pool: dict[int, int] = {}
        for r in rows:
            m = guild.get_member(r["user_id"]) if guild else None
            # Winner verification: still in the server and still meets requirements.
            if m and not m.bot and await self.check_requirements(m, g) is None:
                pool[r["user_id"]] = r["entries"]
        return pick_winners(pool, count, exclude)

    async def end(self, gid: int) -> list[int]:
        g = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
        if not g or g["status"] != "running":
            return []
        winners = await self.draw(g, g["winner_count"], set())
        await self.bot.db.execute("UPDATE giveaways SET status='ended', winners=?, ends_at=? WHERE id=?",
                                  (jdump(winners), min(g["ends_at"], to_db_time(utcnow())), gid))
        g = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
        await self.refresh_message(g)
        await self.announce(g, winners)
        guild = self.bot.get_guild(g["guild_id"])
        if guild:
            self.bot.trigger(guild, "giveaway_ended", prize=g["prize"], winners=", ".join(f"<@{w}>" for w in winners))
        return winners

    async def announce(self, g: dict, winners: list[int], reroll: bool = False) -> None:
        guild = self.bot.get_guild(g["guild_id"])
        ch = guild.get_channel(g["channel_id"]) if guild else None
        if not isinstance(ch, discord.TextChannel):
            return
        ref = discord.MessageReference(message_id=g["message_id"], channel_id=ch.id, fail_if_not_exists=False) if g["message_id"] else None
        if winners:
            text = f"🎉 {'New winner' if reroll else 'Congratulations'} {', '.join(f'<@{w}>' for w in winners)}! You won **{g['prize']}**!"
        else:
            text = f"No valid entries for **{g['prize']}**, so no winner could be chosen."
        try:
            await ch.send(text, reference=ref, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass
        s = await self.bot.settings.get(guild.id, "giveaways")
        if s["dm_winners"]:
            for w in winners:
                m = guild.get_member(w)
                if m:
                    try:
                        await m.send(embed=brand_embed("🎉 You won a giveaway!", f"You won **{g['prize']}** in **{guild.name}**!", SUCCESS))
                    except discord.HTTPException:
                        pass

    async def reroll(self, gid: int, count: int = 1) -> list[int]:
        g = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
        if not g or g["status"] != "ended":
            return []
        prev = set(jload(g["winners"], []))
        new = await self.draw(g, count, prev)
        await self.bot.db.execute("UPDATE giveaways SET winners=? WHERE id=?", (jdump(list(prev) + new), gid))
        await self.announce(g, new, reroll=True)
        return new

    async def cancel(self, gid: int) -> bool:
        g = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
        if not g or g["status"] not in {"running", "scheduled"}:
            return False
        await self.bot.db.execute("UPDATE giveaways SET status='cancelled' WHERE id=?", (gid,))
        g["status"] = "cancelled"
        await self.refresh_message(g)
        return True

    async def extend(self, gid: int, by: timedelta) -> bool:
        g = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
        if not g or g["status"] not in {"running", "scheduled"}:
            return False
        new_end = from_db_time(g["ends_at"]) + by
        await self.bot.db.execute("UPDATE giveaways SET ends_at=? WHERE id=?", (to_db_time(new_end), gid))
        g["ends_at"] = to_db_time(new_end)
        await self.refresh_message(g)
        return True

    @tasks.loop(seconds=15)
    async def loop(self):
        now = to_db_time(utcnow())
        for g in await self.bot.db.fetchall("SELECT id FROM giveaways WHERE status='scheduled' AND starts_at <= ?", (now,)):
            await self.post(g["id"])
        for g in await self.bot.db.fetchall("SELECT id FROM giveaways WHERE status='running' AND ends_at <= ?", (now,)):
            try:
                await self.end(g["id"])
            except Exception as exc:  # keep the loop alive
                await self.bot.db.log_error("giveaways.end", repr(exc))

    @loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ commands
    async def _is_manager(self, interaction: discord.Interaction) -> bool:
        m = interaction.user
        if m.guild_permissions.manage_guild:  # type: ignore[union-attr]
            return True
        s = await self.bot.settings.get(interaction.guild_id, "giveaways")
        if await has_any_role(m, s["manager_role_ids"]):  # type: ignore[arg-type]
            return True
        await interaction.response.send_message(embed=err("You need Manage Server or a giveaway manager role."), ephemeral=True)
        return False

    giveaway = app_commands.Group(name="giveaway", description="Giveaway commands", guild_only=True)

    @giveaway.command(name="start", description="Start a giveaway")
    @app_commands.describe(duration="e.g. 1h, 2d, 1w", starts_in="Schedule for later, e.g. 2h (optional)",
                           bonus_role="Role that gets extra entries", bonus_entries="Extra entries for bonus role")
    async def start(self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200], duration: str,
                    winners: app_commands.Range[int, 1, 50] = 1, channel: discord.TextChannel | None = None,
                    description: str | None = None, required_role: discord.Role | None = None,
                    bonus_role: discord.Role | None = None, bonus_entries: app_commands.Range[int, 1, 100] = 1,
                    min_account_age_days: app_commands.Range[int, 0, 3650] = 0, min_level: app_commands.Range[int, 0, 1000] = 0,
                    starts_in: str | None = None):
        if not await self._is_manager(interaction):
            return
        d = parse_duration(duration)
        if not d or d < timedelta(seconds=30) or d > timedelta(days=60):
            return await interaction.response.send_message(embed=err("Duration must be between 30s and 60d (e.g. `2d`)."), ephemeral=True)
        start_at = None
        if starts_in:
            delay = parse_duration(starts_in)
            if not delay:
                return await interaction.response.send_message(embed=err("Invalid starts_in."), ephemeral=True)
            start_at = utcnow() + delay
        ch = channel or interaction.channel
        gid = await self.create(interaction.guild, ch.id, interaction.user.id, prize, d, winners, description or "",
                                [required_role.id] if required_role else [], {bonus_role.id: bonus_entries} if bonus_role else {},
                                min_account_age_days, min_level, start_at)
        when = f" starting {discord.utils.format_dt(start_at, 'R')}" if start_at else ""
        await interaction.response.send_message(embed=ok(f"Giveaway #{gid} for **{prize}** created in {ch.mention}{when}."), ephemeral=True)

    @giveaway.command(name="end", description="End a giveaway now")
    async def end_cmd(self, interaction: discord.Interaction, giveaway_id: int):
        if not await self._is_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        g = await self.bot.db.fetchone("SELECT status FROM giveaways WHERE id=? AND guild_id=?", (giveaway_id, interaction.guild_id))
        if not g or g["status"] != "running":
            return await interaction.followup.send(embed=err("No running giveaway with that ID."), ephemeral=True)
        w = await self.end(giveaway_id)
        await interaction.followup.send(embed=ok(f"Ended. Winners: {', '.join(f'<@{x}>' for x in w) or 'none'}"), ephemeral=True)

    @giveaway.command(name="reroll", description="Pick new winner(s) for an ended giveaway")
    async def reroll_cmd(self, interaction: discord.Interaction, giveaway_id: int, count: app_commands.Range[int, 1, 20] = 1):
        if not await self._is_manager(interaction):
            return
        g = await self.bot.db.fetchone("SELECT status FROM giveaways WHERE id=? AND guild_id=?", (giveaway_id, interaction.guild_id))
        if not g or g["status"] != "ended":
            return await interaction.response.send_message(embed=err("Only ended giveaways can be rerolled."), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        w = await self.reroll(giveaway_id, count)
        await interaction.followup.send(embed=ok(f"Rerolled: {', '.join(f'<@{x}>' for x in w) or 'no eligible entrants left'}"), ephemeral=True)

    @giveaway.command(name="cancel", description="Cancel a giveaway")
    async def cancel_cmd(self, interaction: discord.Interaction, giveaway_id: int):
        if not await self._is_manager(interaction):
            return
        g = await self.bot.db.fetchone("SELECT id FROM giveaways WHERE id=? AND guild_id=?", (giveaway_id, interaction.guild_id))
        if not g or not await self.cancel(giveaway_id):
            return await interaction.response.send_message(embed=err("Nothing to cancel."), ephemeral=True)
        await interaction.response.send_message(embed=ok(f"Giveaway #{giveaway_id} cancelled."), ephemeral=True)

    @giveaway.command(name="extend", description="Extend a running giveaway")
    async def extend_cmd(self, interaction: discord.Interaction, giveaway_id: int, by: str):
        if not await self._is_manager(interaction):
            return
        d = parse_duration(by)
        g = await self.bot.db.fetchone("SELECT id FROM giveaways WHERE id=? AND guild_id=?", (giveaway_id, interaction.guild_id))
        if not d or not g or not await self.extend(giveaway_id, d):
            return await interaction.response.send_message(embed=err("Couldn't extend (check the ID and duration)."), ephemeral=True)
        await interaction.response.send_message(embed=ok(f"Extended by {human_duration(d.total_seconds())}."), ephemeral=True)

    @giveaway.command(name="list", description="List active and recent giveaways")
    async def list_cmd(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT * FROM giveaways WHERE guild_id=? ORDER BY id DESC LIMIT 15", (interaction.guild_id,))
        lines = [f"`#{r['id']}` **{truncate(r['prize'], 50)}** · {r['status']} · ends {discord.utils.format_dt(from_db_time(r['ends_at']), 'R')}" for r in rows]
        await interaction.response.send_message(embed=brand_embed("Giveaways", "\n".join(lines) or "No giveaways yet."), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Giveaways(bot))
