"""Community systems: suggestions, polls, starboard, counting, birthdays, server-stat channels."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from nexora.bot.helpers import manager_check
from nexora.core.utils import BRAND, DANGER, SUCCESS, WARNING, brand_embed, err, ok, parse_duration, render, truncate
from nexora.db import from_db_time, jdump, jload, to_db_time, utcnow

SUGGESTION_STATUS = {"open": (BRAND, "Open"), "approved": (SUCCESS, "Approved"), "denied": (DANGER, "Denied"),
                     "considering": (WARNING, "Under consideration"), "implemented": (0x22D3EE, "Implemented")}


# ================================================================== suggestions
class SuggestionVote(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:sg:(?P<v>up|down):(?P<id>\d+)"):
    def __init__(self, vote: str, sid: int, count: int = 0):
        super().__init__(discord.ui.Button(label=str(count), emoji="👍" if vote == "up" else "👎",
                                           style=discord.ButtonStyle.success if vote == "up" else discord.ButtonStyle.danger,
                                           custom_id=f"nx:sg:{vote}:{sid}"))
        self.vote, self.sid = vote, sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["v"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        cog: Community = interaction.client.get_cog("Community")  # type: ignore[assignment]
        sug = await cog.bot.db.fetchone("SELECT * FROM suggestions WHERE id=? AND guild_id=?", (self.sid, interaction.guild_id))
        if not sug or sug["status"] != "open":
            return await interaction.response.send_message(embed=err("Voting is closed for this suggestion."), ephemeral=True)
        v = 1 if self.vote == "up" else -1
        prev = await cog.bot.db.fetchval("SELECT vote FROM suggestion_votes WHERE suggestion_id=? AND user_id=?", (self.sid, interaction.user.id))
        if prev == v:
            await cog.bot.db.execute("DELETE FROM suggestion_votes WHERE suggestion_id=? AND user_id=?", (self.sid, interaction.user.id))
        else:
            await cog.bot.db.execute("""INSERT INTO suggestion_votes (suggestion_id, user_id, vote) VALUES (?, ?, ?)
                                        ON CONFLICT(suggestion_id, user_id) DO UPDATE SET vote=excluded.vote""", (self.sid, interaction.user.id, v))
        embed, view = await cog.suggestion_message(sug)
        await interaction.response.edit_message(embed=embed, view=view)


# ======================================================================== polls
class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:poll:(?P<id>\d+):(?P<i>\d+)"):
    def __init__(self, pid: int, index: int, label: str = "Option"):
        super().__init__(discord.ui.Button(label=truncate(label, 80), style=discord.ButtonStyle.secondary, custom_id=f"nx:poll:{pid}:{index}"))
        self.pid, self.index = pid, index

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]), int(match["i"]))

    async def callback(self, interaction: discord.Interaction):
        cog: Community = interaction.client.get_cog("Community")  # type: ignore[assignment]
        poll = await cog.bot.db.fetchone("SELECT * FROM polls WHERE id=?", (self.pid,))
        if not poll or poll["status"] != "open":
            return await interaction.response.send_message(embed=err("This poll has ended."), ephemeral=True)
        db = cog.bot.db
        uid = interaction.user.id
        has = await db.fetchval("SELECT 1 FROM poll_votes WHERE poll_id=? AND user_id=? AND option_index=?", (self.pid, uid, self.index))
        if has:
            await db.execute("DELETE FROM poll_votes WHERE poll_id=? AND user_id=? AND option_index=?", (self.pid, uid, self.index))
            msg = "Vote removed."
        else:
            if not poll["multi"]:
                await db.execute("DELETE FROM poll_votes WHERE poll_id=? AND user_id=?", (self.pid, uid))
            await db.execute("INSERT INTO poll_votes (poll_id, user_id, option_index) VALUES (?, ?, ?)", (self.pid, uid, self.index))
            msg = f"Voted for **{jload(poll['options'], [])[self.index]}**."
        await interaction.response.send_message(embed=ok(msg), ephemeral=True)
        await cog.refresh_poll(poll)


class Community(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_dynamic_items(SuggestionVote, PollButton)
        self.poll_loop.start()
        self.birthday_loop.start()
        self.stats_loop.start()

    def cog_unload(self):
        self.poll_loop.cancel()
        self.birthday_loop.cancel()
        self.stats_loop.cancel()

    # ---------------------------------------------------------- suggestions
    async def suggestion_message(self, sug: dict) -> tuple[discord.Embed, discord.ui.View | None]:
        s = await self.bot.settings.get(sug["guild_id"], "suggestions")
        votes = await self.bot.db.fetchall("SELECT vote FROM suggestion_votes WHERE suggestion_id=?", (sug["id"],))
        up = sum(1 for v in votes if v["vote"] > 0)
        down = len(votes) - up
        color, label = SUGGESTION_STATUS.get(sug["status"], (BRAND, sug["status"]))
        embed = brand_embed(f"Suggestion #{sug['number']}", sug["content"], color)
        if not s["anonymous"]:
            user = self.bot.get_user(sug["user_id"])
            embed.set_author(name=str(user) if user else f"User {sug['user_id']}", icon_url=user.display_avatar.url if user else None)
        embed.add_field(name="Status", value=label)
        embed.add_field(name="Votes", value=f"👍 {up} · 👎 {down}")
        if sug["response"]:
            embed.add_field(name="Staff response", value=f"{sug['response']}" + (f" — <@{sug['responder_id']}>" if sug["responder_id"] else ""), inline=False)
        view = None
        if sug["status"] == "open":
            view = discord.ui.View(timeout=None)
            view.add_item(SuggestionVote("up", sug["id"], up))
            view.add_item(SuggestionVote("down", sug["id"], down))
        return embed, view

    async def create_suggestion(self, guild: discord.Guild, user: discord.abc.User, content: str) -> dict:
        s = await self.bot.settings.get(guild.id, "suggestions")
        ch = guild.get_channel(s["channel_id"] or 0)
        if not s["enabled"] or not isinstance(ch, discord.TextChannel):
            raise ValueError("Suggestions aren't set up on this server.")
        number = await self.bot.db.next_number("suggestions", "number", guild.id)
        sid = await self.bot.db.execute("INSERT INTO suggestions (guild_id, number, user_id, content, channel_id) VALUES (?, ?, ?, ?, ?)",
                                        (guild.id, number, user.id, content, ch.id))
        sug = await self.bot.db.fetchone("SELECT * FROM suggestions WHERE id=?", (sid,))
        embed, view = await self.suggestion_message(sug)
        msg = await ch.send(embed=embed, view=view)
        await self.bot.db.execute("UPDATE suggestions SET message_id=? WHERE id=?", (msg.id, sid))
        if s["create_threads"]:
            try:
                await msg.create_thread(name=f"Suggestion #{number}", auto_archive_duration=10080)
            except discord.HTTPException:
                pass
        return sug

    async def respond_suggestion(self, guild: discord.Guild, number: int, status: str, response: str | None, responder: discord.abc.User) -> str:
        sug = await self.bot.db.fetchone("SELECT * FROM suggestions WHERE guild_id=? AND number=?", (guild.id, number))
        if not sug:
            return "Suggestion not found."
        await self.bot.db.execute("UPDATE suggestions SET status=?, response=?, responder_id=? WHERE id=?", (status, response, responder.id, sug["id"]))
        sug.update(status=status, response=response, responder_id=responder.id)
        ch = guild.get_channel(sug["channel_id"] or 0)
        if isinstance(ch, discord.TextChannel) and sug["message_id"]:
            embed, view = await self.suggestion_message(sug)
            try:
                await ch.get_partial_message(sug["message_id"]).edit(embed=embed, view=view)
            except discord.HTTPException:
                pass
        s = await self.bot.settings.get(guild.id, "suggestions")
        user = self.bot.get_user(sug["user_id"])
        if s["dm_on_response"] and user:
            try:
                await user.send(embed=brand_embed(f"Your suggestion #{number} in {guild.name}: {SUGGESTION_STATUS[status][1]}",
                                                  f"> {truncate(sug['content'], 500)}\n\n{response or ''}", SUGGESTION_STATUS[status][0]))
            except discord.HTTPException:
                pass
        await self.bot.track_staff(guild.id, responder.id)
        return f"Suggestion #{number} marked **{SUGGESTION_STATUS[status][1]}**."

    @app_commands.command(description="Make a suggestion")
    @app_commands.guild_only()
    async def suggest(self, interaction: discord.Interaction, suggestion: app_commands.Range[str, 10, 2000]):
        try:
            sug = await self.create_suggestion(interaction.guild, interaction.user, suggestion)
        except ValueError as exc:
            return await interaction.response.send_message(embed=err(str(exc)), ephemeral=True)
        self.bot.trigger(interaction.guild, "suggestion_created", member=interaction.user, number=sug["number"])
        await interaction.response.send_message(embed=ok(f"Suggestion #{sug['number']} posted!"), ephemeral=True)

    @app_commands.command(name="suggestion", description="Respond to a suggestion (staff)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.choices(status=[app_commands.Choice(name=v[1], value=k) for k, v in SUGGESTION_STATUS.items()])
    async def suggestion_respond(self, interaction: discord.Interaction, number: int, status: app_commands.Choice[str], response: str | None = None):
        msg = await self.respond_suggestion(interaction.guild, number, status.value, response, interaction.user)
        await interaction.response.send_message(embed=ok(msg), ephemeral=True)

    # ---------------------------------------------------------------- polls
    async def poll_embed(self, poll: dict) -> discord.Embed:
        options = jload(poll["options"], [])
        rows = await self.bot.db.fetchall("SELECT option_index, COUNT(*) AS n FROM poll_votes WHERE poll_id=? GROUP BY option_index", (poll["id"],))
        counts = {r["option_index"]: r["n"] for r in rows}
        voters = await self.bot.db.fetchval("SELECT COUNT(DISTINCT user_id) FROM poll_votes WHERE poll_id=?", (poll["id"],), 0)
        total = sum(counts.values()) or 1
        lines = []
        top = max(counts.values()) if counts else -1
        for i, o in enumerate(options):
            n = counts.get(i, 0)
            bar = "█" * round(n / total * 16) + "░" * (16 - round(n / total * 16))
            crown = " 👑" if poll["status"] == "closed" and n == top and n > 0 else ""
            lines.append(f"**{o}**{crown}\n`{bar}` {n} ({n / total:.0%})")
        embed = brand_embed(f"📊 {poll['question']}", "\n\n".join(lines), BRAND if poll["status"] == "open" else 0x64748B)
        foot = f"{voters} voter(s)" + (" · multiple choice" if poll["multi"] else "")
        if poll["ends_at"] and poll["status"] == "open":
            embed.add_field(name="Ends", value=discord.utils.format_dt(from_db_time(poll["ends_at"]), "R"))
        if poll["status"] == "closed":
            foot += " · poll closed"
        embed.set_footer(text=foot)
        return embed

    def poll_view(self, poll: dict) -> discord.ui.View | None:
        if poll["status"] != "open":
            return None
        view = discord.ui.View(timeout=None)
        for i, o in enumerate(jload(poll["options"], [])[:20]):
            view.add_item(PollButton(poll["id"], i, o))
        return view

    async def refresh_poll(self, poll: dict):
        poll = await self.bot.db.fetchone("SELECT * FROM polls WHERE id=?", (poll["id"],))
        guild = self.bot.get_guild(poll["guild_id"])
        ch = guild.get_channel(poll["channel_id"]) if guild else None
        if isinstance(ch, (discord.TextChannel, discord.Thread)) and poll["message_id"]:
            try:
                await ch.get_partial_message(poll["message_id"]).edit(embed=await self.poll_embed(poll), view=self.poll_view(poll))
            except discord.HTTPException:
                pass

    async def create_poll(self, guild: discord.Guild, channel: discord.abc.Messageable, author_id: int, question: str,
                          options: list[str], multi: bool, duration: timedelta | None) -> int:
        pid = await self.bot.db.execute(
            "INSERT INTO polls (guild_id, channel_id, author_id, question, options, multi, ends_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild.id, channel.id, author_id, question, jdump(options), int(multi), to_db_time(utcnow() + duration) if duration else None))
        poll = await self.bot.db.fetchone("SELECT * FROM polls WHERE id=?", (pid,))
        msg = await channel.send(embed=await self.poll_embed(poll), view=self.poll_view(poll))
        await self.bot.db.execute("UPDATE polls SET message_id=? WHERE id=?", (msg.id, pid))
        return pid

    @app_commands.command(description="Create a poll (separate options with |)")
    @app_commands.guild_only()
    @app_commands.describe(options="e.g. Pizza | Burgers | Tacos (2-20 options)", duration="e.g. 1h, 2d — empty = until ended manually")
    async def poll(self, interaction: discord.Interaction, question: app_commands.Range[str, 3, 250], options: str,
                   duration: str | None = None, multiple_choice: bool = False):
        opts = [o.strip() for o in options.split("|") if o.strip()]
        if not 2 <= len(opts) <= 20:
            return await interaction.response.send_message(embed=err("Give between 2 and 20 options separated by `|`."), ephemeral=True)
        d = parse_duration(duration) if duration else None
        if duration and not d:
            return await interaction.response.send_message(embed=err("Invalid duration."), ephemeral=True)
        await interaction.response.send_message(embed=ok("Poll created."), ephemeral=True)
        await self.create_poll(interaction.guild, interaction.channel, interaction.user.id, question, [truncate(o, 80) for o in opts], multiple_choice, d)
        await self.bot.track_feature(interaction.guild_id, "polls:create")

    @app_commands.command(name="endpoll", description="End a poll by its message link or ID")
    @app_commands.guild_only()
    async def endpoll(self, interaction: discord.Interaction, message_id: str):
        mid = int(message_id.rstrip("/").split("/")[-1]) if message_id.rstrip("/").split("/")[-1].isdigit() else 0
        poll = await self.bot.db.fetchone("SELECT * FROM polls WHERE guild_id=? AND message_id=?", (interaction.guild_id, mid))
        if not poll:
            return await interaction.response.send_message(embed=err("Poll not found."), ephemeral=True)
        if poll["author_id"] != interaction.user.id and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message(embed=err("Only the poll creator or staff can end it."), ephemeral=True)
        await self.bot.db.execute("UPDATE polls SET status='closed' WHERE id=?", (poll["id"],))
        await self.refresh_poll(poll)
        await interaction.response.send_message(embed=ok("Poll ended."), ephemeral=True)

    @tasks.loop(seconds=30)
    async def poll_loop(self):
        for p in await self.bot.db.fetchall("SELECT * FROM polls WHERE status='open' AND ends_at IS NOT NULL AND ends_at <= ?", (to_db_time(utcnow()),)):
            await self.bot.db.execute("UPDATE polls SET status='closed' WHERE id=?", (p["id"],))
            await self.refresh_poll(p)

    # ------------------------------------------------------------ starboard
    async def _starboard(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return
        s = await self.bot.settings.get(payload.guild_id, "starboard")
        if not s["enabled"] or not s["channel_id"] or str(payload.emoji) != s["emoji"] or payload.channel_id in s["ignored_channel_ids"]:
            return
        if payload.channel_id == s["channel_id"]:
            return
        guild = self.bot.get_guild(payload.guild_id)
        src = guild.get_channel_or_thread(payload.channel_id) if guild else None
        board = guild.get_channel(s["channel_id"]) if guild else None
        if not isinstance(board, discord.TextChannel) or src is None:
            return
        try:
            msg = await src.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if getattr(src, "nsfw", False) and not board.nsfw:
            return
        reaction = discord.utils.find(lambda r: str(r.emoji) == s["emoji"], msg.reactions)
        count = 0
        if reaction:
            count = reaction.count
            if not s["allow_self_star"]:
                users = [u async for u in reaction.users(limit=None)]
                count = sum(1 for u in users if u.id != msg.author.id)
        row = await self.bot.db.fetchone("SELECT * FROM starboard_posts WHERE guild_id=? AND message_id=?", (guild.id, msg.id))
        content = f"{s['emoji']} **{count}** · {src.mention}"
        if count >= s["threshold"]:
            embed = brand_embed(description=msg.content or None, color=0xFACC15)
            embed.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
            embed.add_field(name="Source", value=f"[Jump to message]({msg.jump_url})")
            img = next((a.url for a in msg.attachments if (a.content_type or "").startswith("image")), None)
            if img:
                embed.set_image(url=img)
            embed.timestamp = msg.created_at
            if row and row["star_message_id"]:
                try:
                    await board.get_partial_message(row["star_message_id"]).edit(content=content, embed=embed)
                except discord.HTTPException:
                    pass
                await self.bot.db.execute("UPDATE starboard_posts SET stars=? WHERE guild_id=? AND message_id=?", (count, guild.id, msg.id))
            else:
                posted = await board.send(content=content, embed=embed)
                await self.bot.db.execute(
                    """INSERT INTO starboard_posts (guild_id, message_id, star_message_id, stars) VALUES (?, ?, ?, ?)
                       ON CONFLICT(guild_id, message_id) DO UPDATE SET star_message_id=excluded.star_message_id, stars=excluded.stars""",
                    (guild.id, msg.id, posted.id, count))
        elif row and row["star_message_id"]:
            try:
                await board.get_partial_message(row["star_message_id"]).delete()
            except discord.HTTPException:
                pass
            await self.bot.db.execute("DELETE FROM starboard_posts WHERE guild_id=? AND message_id=?", (guild.id, msg.id))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self._starboard(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self._starboard(payload)

    # ------------------------------------------------------------- counting
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        s = await self.bot.settings.get(message.guild.id, "counting")
        if not s["enabled"] or message.channel.id != s["channel_id"]:
            return
        state = await self.bot.db.fetchone("SELECT * FROM counting_state WHERE guild_id=?", (message.guild.id,)) or {"current": 0, "last_user_id": None, "high_score": 0}
        text = message.content.strip().split()[0] if message.content.strip() else ""
        expected = state["current"] + 1
        wrong_user = not s["allow_consecutive"] and state["last_user_id"] == message.author.id
        if text.isdigit() and int(text) == expected and not wrong_user:
            high = max(state["high_score"], expected)
            await self.bot.db.execute(
                """INSERT INTO counting_state (guild_id, current, last_user_id, high_score) VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET current=excluded.current, last_user_id=excluded.last_user_id, high_score=excluded.high_score""",
                (message.guild.id, expected, message.author.id, high))
            try:
                await message.add_reaction("🎉" if expected % 100 == 0 else "✅")
            except discord.HTTPException:
                pass
            return
        if s["delete_wrong"]:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return
        if s["reset_on_fail"]:
            await self.bot.db.execute(
                """INSERT INTO counting_state (guild_id, current, last_user_id, high_score) VALUES (?, 0, NULL, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET current=0, last_user_id=NULL""", (message.guild.id, state["high_score"]))
            reason = "You can't count twice in a row" if wrong_user else f"The next number was **{expected}**"
            try:
                await message.add_reaction("❌")
                await message.channel.send(f"{message.author.mention} ruined it at **{state['current']}**! {reason}. Start again from **1**. (Record: {state['high_score']})")
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------ birthdays
    birthday = app_commands.Group(name="birthday", description="Birthday commands", guild_only=True)

    @birthday.command(name="set", description="Set your birthday")
    async def birthday_set(self, interaction: discord.Interaction, month: app_commands.Range[int, 1, 12], day: app_commands.Range[int, 1, 31]):
        try:
            date(2024, month, day)  # leap year so Feb 29 is valid
        except ValueError:
            return await interaction.response.send_message(embed=err("That date doesn't exist."), ephemeral=True)
        await self.bot.db.execute("""INSERT INTO birthdays (guild_id, user_id, month, day) VALUES (?, ?, ?, ?)
                                     ON CONFLICT(guild_id, user_id) DO UPDATE SET month=excluded.month, day=excluded.day""",
                                  (interaction.guild_id, interaction.user.id, month, day))
        await interaction.response.send_message(embed=ok(f"Birthday saved: **{date(2024, month, day):%B %d}** 🎂"), ephemeral=True)

    @birthday.command(name="remove", description="Remove your birthday")
    async def birthday_remove(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM birthdays WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id))
        await interaction.response.send_message(embed=ok("Birthday removed."), ephemeral=True)

    @birthday.command(name="list", description="Upcoming birthdays")
    async def birthday_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT * FROM birthdays WHERE guild_id=?", (interaction.guild_id,))
        today = date.today()

        def next_date(r):
            for y in (today.year, today.year + 1, today.year + 2):
                try:
                    d = date(y, r["month"], r["day"])
                except ValueError:
                    continue
                if d >= today:
                    return d
            return date.max

        rows.sort(key=next_date)
        lines = [f"**{next_date(r):%b %d}** — <@{r['user_id']}>" for r in rows[:15]]
        await interaction.response.send_message(embed=brand_embed("Upcoming birthdays", "\n".join(lines) or "No birthdays saved."),
                                                allowed_mentions=discord.AllowedMentions.none())

    @tasks.loop(minutes=15)
    async def birthday_loop(self):
        now = datetime.now(timezone.utc)
        today = now.date()
        for guild in self.bot.guilds:
            s = await self.bot.settings.get(guild.id, "birthdays")
            if not s["enabled"]:
                continue
            role = guild.get_role(s["role_id"]) if s["role_id"] else None
            rows = await self.bot.db.fetchall("SELECT * FROM birthdays WHERE guild_id=?", (guild.id,))
            for r in rows:
                is_today = (r["month"], r["day"]) == (today.month, today.day) or (
                    r["month"] == 2 and r["day"] == 29 and (today.month, today.day) == (2, 28) and not _is_leap(today.year))
                member = guild.get_member(r["user_id"])
                if not member:
                    continue
                if role and not is_today and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Birthday over")
                    except discord.HTTPException:
                        pass
                if is_today and now.hour >= s["announce_hour_utc"] and r["last_announced_year"] != today.year:
                    await self.bot.db.execute("UPDATE birthdays SET last_announced_year=? WHERE guild_id=? AND user_id=?", (today.year, guild.id, member.id))
                    ch = guild.get_channel(s["channel_id"] or 0)
                    if isinstance(ch, discord.TextChannel):
                        try:
                            await ch.send(render(s["message"], member, guild), allowed_mentions=discord.AllowedMentions(users=True))
                        except discord.HTTPException:
                            pass
                    if role:
                        try:
                            await member.add_roles(role, reason="Birthday!")
                        except discord.HTTPException:
                            pass

    # --------------------------------------------------------- stat channels
    @tasks.loop(minutes=10)
    async def stats_loop(self):
        for guild in self.bot.guilds:
            s = await self.bot.settings.get(guild.id, "server_stats")
            if not s["enabled"]:
                continue
            humans = sum(1 for m in guild.members if not m.bot)
            values = {"members": guild.member_count or 0, "humans": humans, "bots": (guild.member_count or 0) - humans,
                      "boosts": guild.premium_subscription_count or 0}
            for key, count in values.items():
                cid = s[f"{key}_channel_id"]
                ch = guild.get_channel(cid) if cid else None
                if ch is None:
                    continue
                name = truncate(s[f"{key}_format"].replace("{count}", f"{count:,}"), 100)
                if ch.name != name:
                    try:
                        await ch.edit(name=name, reason="Nexora server stats")
                    except discord.HTTPException:
                        pass

    @poll_loop.before_loop
    @birthday_loop.before_loop
    @stats_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


def _is_leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


async def setup(bot):
    await bot.add_cog(Community(bot))
