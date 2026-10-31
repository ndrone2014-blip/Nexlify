"""Applications: multi-page forms (short/long/multiple-choice), cooldowns, limits, multi-reviewer votes,
notes, automatic roles and DMs."""
from __future__ import annotations

import io
import re
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from nexora.bot.helpers import has_any_role, log_event
from nexora.core.utils import DANGER, SUCCESS, WARNING, brand_embed, err, ok, render, truncate
from nexora.db import from_db_time, jdump, jload, utcnow

FORM_KINDS = ["staff", "moderator", "administrator", "developer", "media", "partnership", "custom"]


def chunk_steps(questions: list[dict]) -> list[list[int]]:
    """Group question indexes into steps: each choice question is its own step; up to 5 text questions per modal."""
    steps: list[list[int]] = []
    current: list[int] = []
    for i, q in enumerate(questions):
        if q.get("type") == "choice":
            if current:
                steps.append(current)
                current = []
            steps.append([i])
        else:
            current.append(i)
            if len(current) == 5:
                steps.append(current)
                current = []
    if current:
        steps.append(current)
    return steps


class ApplySession:
    def __init__(self, form: dict):
        self.form = form
        self.questions: list[dict] = jload(form["questions"], [])
        self.steps = chunk_steps(self.questions)
        self.step = 0
        self.answers: dict[int, str] = {}
        self.started = time.time()


class ApplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:apply:(?P<form>\d+)"):
    def __init__(self, form_id: int, label: str = "Apply"):
        super().__init__(discord.ui.Button(label=label, emoji="📝", style=discord.ButtonStyle.primary, custom_id=f"nx:apply:{form_id}"))
        self.form_id = form_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["form"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Applications").begin(interaction, self.form_id)  # type: ignore[union-attr]


class ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nx:app:(?P<action>accept|deny|denyr|note):(?P<id>\d+)"):
    STYLES = {"accept": ("Accept", discord.ButtonStyle.success), "deny": ("Deny", discord.ButtonStyle.danger),
              "denyr": ("Deny with reason", discord.ButtonStyle.secondary), "note": ("Add note", discord.ButtonStyle.secondary)}

    def __init__(self, action: str, sub_id: int):
        label, style = self.STYLES[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"nx:app:{action}:{sub_id}"))
        self.action, self.sub_id = action, sub_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        cog: Applications = interaction.client.get_cog("Applications")  # type: ignore[assignment]
        sub = await cog.bot.db.fetchone("SELECT * FROM application_submissions WHERE id=?", (self.sub_id,))
        form = await cog.bot.db.fetchone("SELECT * FROM application_forms WHERE id=?", (sub["form_id"],)) if sub else None
        if not sub or not form:
            return await interaction.response.send_message(embed=err("Submission not found."), ephemeral=True)
        if not await cog.can_review(interaction.user, form):  # type: ignore[arg-type]
            return await interaction.response.send_message(embed=err("You aren't a reviewer for this form."), ephemeral=True)
        if self.action == "note":
            return await interaction.response.send_modal(NoteModal(cog, self.sub_id))
        if self.action == "denyr":
            return await interaction.response.send_modal(DenyModal(cog, self.sub_id))
        await interaction.response.defer(ephemeral=True)
        msg = await cog.vote(interaction.guild, self.sub_id, interaction.user, self.action == "accept", None)
        await interaction.followup.send(embed=ok(msg), ephemeral=True)


class NoteModal(discord.ui.Modal, title="Reviewer note"):
    note = discord.ui.TextInput(label="Note (only visible to reviewers)", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, cog, sub_id):
        super().__init__()
        self.cog, self.sub_id = cog, sub_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.add_note(self.sub_id, interaction.user, self.note.value)
        await interaction.response.send_message(embed=ok("Note added."), ephemeral=True)


class DenyModal(discord.ui.Modal, title="Deny application"):
    reason = discord.ui.TextInput(label="Reason (sent to the applicant)", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, cog, sub_id):
        super().__init__()
        self.cog, self.sub_id = cog, sub_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        msg = await self.cog.vote(interaction.guild, self.sub_id, interaction.user, False, self.reason.value)
        await interaction.followup.send(embed=ok(msg), ephemeral=True)


class TextStepModal(discord.ui.Modal):
    def __init__(self, cog: "Applications", session: ApplySession):
        idxs = session.steps[session.step]
        super().__init__(title=truncate(f"{session.form['name']} ({session.step + 1}/{len(session.steps)})", 45), timeout=900)
        self.cog, self.session, self.idxs = cog, session, idxs
        self.inputs: list[discord.ui.TextInput] = []
        for i in idxs:
            q = session.questions[i]
            long = q.get("type") == "long"
            ti = discord.ui.TextInput(
                label=truncate(q.get("label") or f"Question {i + 1}", 45),
                placeholder=truncate(q.get("placeholder") or "", 100) or None,
                required=bool(q.get("required", True)),
                style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
                min_length=int(q.get("min_length") or 0) or None,
                max_length=min(int(q.get("max_length") or (2000 if long else 300)), 4000),
                default=session.answers.get(i),
            )
            self.add_item(ti)
            self.inputs.append(ti)

    async def on_submit(self, interaction: discord.Interaction):
        for i, ti in zip(self.idxs, self.inputs):
            self.session.answers[i] = ti.value
        self.session.step += 1
        await self.cog.advance(interaction, self.session, after_modal=True)


class ChoiceStepView(discord.ui.View):
    def __init__(self, cog: "Applications", session: ApplySession):
        super().__init__(timeout=900)
        self.cog, self.session = cog, session
        i = session.steps[session.step][0]
        q = session.questions[i]
        opts = [o for o in (q.get("options") or []) if o][:25]
        select = discord.ui.Select(placeholder=truncate(q.get("label") or "Choose", 150), min_values=1 if q.get("required", True) else 0,
                                   max_values=1, options=[discord.SelectOption(label=truncate(o, 100), value=str(n)) for n, o in enumerate(opts)])
        select.callback = self.on_select  # type: ignore[method-assign]
        self.select, self.q_index, self.opts = select, i, opts
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        vals = self.select.values
        self.session.answers[self.q_index] = self.opts[int(vals[0])] if vals else ""
        self.session.step += 1
        await self.cog.advance(interaction, self.session, after_modal=False)


class ContinueView(discord.ui.View):
    def __init__(self, cog: "Applications", session: ApplySession):
        super().__init__(timeout=900)
        self.cog, self.session = cog, session

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary, emoji="➡️")
    async def cont(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(TextStepModal(self.cog, self.session))


class Applications(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sessions: dict[tuple[int, int], ApplySession] = {}
        bot.add_dynamic_items(ApplyButton, ReviewButton)

    async def can_review(self, member: discord.Member, form: dict) -> bool:
        if member.guild_permissions.manage_guild:
            return True
        return await has_any_role(member, jload(form["reviewer_role_ids"], []))

    async def eligibility(self, guild_id: int, user_id: int, form: dict) -> str | None:
        s = await self.bot.settings.get(guild_id, "applications")
        if not s["enabled"] or not form["enabled"]:
            return "This application is closed."
        if not jload(form["questions"], []):
            return "This form has no questions yet."
        pending = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM application_submissions WHERE form_id=? AND user_id=? AND status='pending'", (form["id"], user_id), 0)
        if pending:
            return "You already have a pending application for this form."
        total = await self.bot.db.fetchval("SELECT COUNT(*) FROM application_submissions WHERE form_id=? AND user_id=?", (form["id"], user_id), 0)
        if form["max_submissions"] and total >= form["max_submissions"]:
            return f"You've reached the limit of {form['max_submissions']} submissions for this form."
        last = await self.bot.db.fetchval("SELECT MAX(created_at) FROM application_submissions WHERE form_id=? AND user_id=?", (form["id"], user_id))
        if last and form["cooldown_hours"]:
            ready = from_db_time(last) + timedelta(hours=form["cooldown_hours"])
            if ready > utcnow():
                return f"You can apply again {discord.utils.format_dt(ready, 'R')}."
        return None

    async def begin(self, interaction: discord.Interaction, form_id: int):
        form = await self.bot.db.fetchone("SELECT * FROM application_forms WHERE id=? AND guild_id=?", (form_id, interaction.guild_id))
        if not form:
            return await interaction.response.send_message(embed=err("Form not found."), ephemeral=True)
        problem = await self.eligibility(interaction.guild_id, interaction.user.id, form)
        if problem:
            return await interaction.response.send_message(embed=err(problem), ephemeral=True)
        session = ApplySession(form)
        self.sessions[(interaction.guild_id, interaction.user.id)] = session
        await self.advance(interaction, session, after_modal=False, first=True)

    async def advance(self, interaction: discord.Interaction, session: ApplySession, after_modal: bool, first: bool = False):
        if session.step >= len(session.steps):
            return await self.finish(interaction, session)
        idxs = session.steps[session.step]
        is_choice = session.questions[idxs[0]].get("type") == "choice"
        progress = f"Step {session.step + 1} of {len(session.steps)}"
        if is_choice:
            embed = brand_embed(session.form["name"], f"**{session.questions[idxs[0]].get('label')}**\n{progress}")
            view = ChoiceStepView(self, session)
            if first or after_modal:
                return await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return await interaction.response.edit_message(embed=embed, view=view)
        if first or not after_modal:
            # a modal can be the response to a button/select/slash interaction
            return await interaction.response.send_modal(TextStepModal(self, session))
        # a modal submit can't open another modal, so ask the user to click Continue
        await interaction.response.send_message(embed=brand_embed(session.form["name"], f"Saved! {progress} is next."),
                                                view=ContinueView(self, session), ephemeral=True)

    async def finish(self, interaction: discord.Interaction, session: ApplySession):
        guild, user, form = interaction.guild, interaction.user, session.form
        self.sessions.pop((guild.id, user.id), None)
        problem = await self.eligibility(guild.id, user.id, form)
        if problem:
            return await interaction.response.send_message(embed=err(problem), ephemeral=True)
        answers = [{"question": q.get("label"), "answer": session.answers.get(i, "")} for i, q in enumerate(session.questions)]
        sub_id = await self.bot.db.execute(
            "INSERT INTO application_submissions (guild_id, form_id, user_id, answers) VALUES (?, ?, ?, ?)",
            (guild.id, form["id"], user.id, jdump(answers)))
        if interaction.response.is_done():
            await interaction.followup.send(embed=ok("Application submitted! You'll be notified of the decision."), ephemeral=True)
        else:
            await interaction.response.send_message(embed=ok("Application submitted! You'll be notified of the decision."), ephemeral=True)
        await self.post_for_review(guild, sub_id)
        s = await self.bot.settings.get(guild.id, "applications")
        if s["dm_on_submit"]:
            try:
                await user.send(embed=brand_embed(f"Application received · {form['name']}", f"Thanks for applying in **{guild.name}**. We'll let you know once it's reviewed."))
            except discord.HTTPException:
                pass
        await self.bot.track_feature(guild.id, "applications:submit")
        self.bot.trigger(guild, "application_submitted", member=user, form=form["name"])

    async def post_for_review(self, guild: discord.Guild, sub_id: int):
        sub = await self.bot.db.fetchone("SELECT * FROM application_submissions WHERE id=?", (sub_id,))
        form = await self.bot.db.fetchone("SELECT * FROM application_forms WHERE id=?", (sub["form_id"],))
        s = await self.bot.settings.get(guild.id, "applications")
        channel = guild.get_channel(form["review_channel_id"] or s["default_review_channel_id"] or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        user = guild.get_member(sub["user_id"]) or self.bot.get_user(sub["user_id"])
        answers = jload(sub["answers"], [])
        embed = brand_embed(f"{form['name']} application #{sub_id}", color=WARNING)
        if user:
            embed.set_author(name=f"{user} ({user.id})", icon_url=user.display_avatar.url)
            if isinstance(user, discord.Member) and user.joined_at:
                embed.description = f"Account created {discord.utils.format_dt(user.created_at, 'R')} · joined {discord.utils.format_dt(user.joined_at, 'R')}"
        total = 0
        files = []
        for a in answers:
            value = truncate(a["answer"], 1024) or "—"
            total += len(value) + len(a["question"] or "")
            if len(embed.fields) >= 24 or total > 5000:
                break
            embed.add_field(name=truncate(a["question"], 256), value=value, inline=False)
        if len(embed.fields) < len(answers) or any(len(a["answer"] or "") > 1024 for a in answers):
            text = "\n\n".join(f"Q: {a['question']}\nA: {a['answer']}" for a in answers)
            files.append(discord.File(io.BytesIO(text.encode()), filename=f"application-{sub_id}.txt"))
            embed.set_footer(text="Full answers attached")
        if form["required_votes"] > 1:
            embed.add_field(name="Votes required", value=str(form["required_votes"]))
        view = discord.ui.View(timeout=None)
        for a in ("accept", "deny", "denyr", "note"):
            view.add_item(ReviewButton(a, sub_id))
        msg = await channel.send(embed=embed, view=view, files=files)
        await self.bot.db.execute("UPDATE application_submissions SET review_message_id=? WHERE id=?", (msg.id, sub_id))

    async def add_note(self, sub_id: int, reviewer: discord.abc.User, note: str):
        await self.bot.db.execute("INSERT INTO application_notes (submission_id, reviewer_id, note) VALUES (?, ?, ?)", (sub_id, reviewer.id, note))

    async def vote(self, guild: discord.Guild, sub_id: int, reviewer: discord.abc.User, accept: bool, reason: str | None) -> str:
        sub = await self.bot.db.fetchone("SELECT * FROM application_submissions WHERE id=? AND guild_id=?", (sub_id, guild.id))
        if not sub:
            return "Submission not found."
        if sub["status"] != "pending":
            return f"Already {sub['status']}."
        form = await self.bot.db.fetchone("SELECT * FROM application_forms WHERE id=?", (sub["form_id"],))
        await self.bot.db.execute(
            """INSERT INTO application_votes (submission_id, reviewer_id, vote) VALUES (?, ?, ?)
               ON CONFLICT(submission_id, reviewer_id) DO UPDATE SET vote=excluded.vote""",
            (sub_id, reviewer.id, "accept" if accept else "deny"))
        if reason:
            await self.add_note(sub_id, reviewer, f"Deny reason: {reason}")
        await self.bot.track_staff(guild.id, reviewer.id)
        votes = await self.bot.db.fetchall("SELECT vote FROM application_votes WHERE submission_id=?", (sub_id,))
        n_acc = sum(1 for v in votes if v["vote"] == "accept")
        n_den = len(votes) - n_acc
        need = max(1, form["required_votes"])
        if n_acc >= need:
            return await self.decide(guild, sub_id, True, reviewer, reason)
        if n_den >= need:
            return await self.decide(guild, sub_id, False, reviewer, reason)
        return f"Vote recorded ({n_acc} accept / {n_den} deny, {need} needed)."

    async def decide(self, guild: discord.Guild, sub_id: int, accept: bool, reviewer: discord.abc.User, reason: str | None) -> str:
        """Finalise a submission. Also called directly by the dashboard."""
        sub = await self.bot.db.fetchone("SELECT * FROM application_submissions WHERE id=? AND guild_id=?", (sub_id, guild.id))
        if not sub or sub["status"] != "pending":
            return "Submission not pending."
        form = await self.bot.db.fetchone("SELECT * FROM application_forms WHERE id=?", (sub["form_id"],))
        status = "accepted" if accept else "denied"
        await self.bot.db.execute(
            "UPDATE application_submissions SET status=?, reviewer_id=?, decision_reason=?, decided_at=datetime('now') WHERE id=?",
            (status, reviewer.id, reason, sub_id))
        notes = []
        member = guild.get_member(sub["user_id"])
        if accept and member:
            add = [guild.get_role(r) for r in jload(form["accept_role_ids"], [])]
            rem = [guild.get_role(r) for r in jload(form["remove_role_ids"], [])]
            try:
                if any(add):
                    await member.add_roles(*[r for r in add if r], reason=f"Application #{sub_id} accepted")
                    notes.append("roles given")
                if any(rem):
                    await member.remove_roles(*[r for r in rem if r], reason=f"Application #{sub_id} accepted")
            except discord.HTTPException:
                notes.append("could not change roles (check my role position)")
        s = await self.bot.settings.get(guild.id, "applications")
        user = member or self.bot.get_user(sub["user_id"])
        if s["dm_on_decision"] and user:
            text = render(form["accept_message"] if accept else form["deny_message"], user, guild, form=form["name"])
            if reason and not accept:
                text += f"\n\n**Reason:** {reason}"
            try:
                await user.send(embed=brand_embed(f"Application {status}", text, SUCCESS if accept else DANGER))
                notes.append("applicant DMed")
            except discord.HTTPException:
                notes.append("applicant has DMs closed")
        channel = guild.get_channel(form["review_channel_id"] or s["default_review_channel_id"] or 0)
        if isinstance(channel, discord.TextChannel) and sub["review_message_id"]:
            try:
                msg = await channel.fetch_message(sub["review_message_id"])
                embed = msg.embeds[0]
                embed.color = SUCCESS if accept else DANGER
                embed.add_field(name="Decision", value=f"**{status.title()}** by {reviewer.mention}" + (f"\nReason: {reason}" if reason else ""), inline=False)
                await msg.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass
        await log_event(self.bot, guild, "server", f"application_{status}", f"Application #{sub_id} ({form['name']}) {status} by {reviewer}",
                        None, user_id=sub["user_id"], moderator_id=reviewer.id)
        if user:
            self.bot.trigger(guild, f"application_{status}", member=user, form=form["name"])
        return f"Application {status}" + (f" ({', '.join(notes)})" if notes else "") + "."

    async def publish_form(self, form_id: int) -> discord.Message:
        form = await self.bot.db.fetchone("SELECT * FROM application_forms WHERE id=?", (form_id,))
        guild = self.bot.get_guild(form["guild_id"]) if form else None
        channel = guild.get_channel(form["panel_channel_id"] or 0) if guild else None
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Set a panel channel for this form first")
        embed = brand_embed(f"📝 {form['name']}", form["description"] or "Click below to apply.")
        embed.add_field(name="Questions", value=str(len(jload(form["questions"], []))))
        if form["cooldown_hours"]:
            embed.add_field(name="Cooldown", value=f"{form['cooldown_hours']}h")
        view = discord.ui.View(timeout=None)
        view.add_item(ApplyButton(form_id, "Apply"))
        if form["panel_message_id"]:
            try:
                msg = await channel.fetch_message(form["panel_message_id"])
                await msg.edit(embed=embed, view=view)
                return msg
            except discord.HTTPException:
                pass
        msg = await channel.send(embed=embed, view=view)
        await self.bot.db.execute("UPDATE application_forms SET panel_message_id=? WHERE id=?", (msg.id, form_id))
        return msg

    # ------------------------------------------------------------ commands
    @app_commands.command(description="Apply for a position")
    @app_commands.guild_only()
    async def apply(self, interaction: discord.Interaction, form: int):
        await self.begin(interaction, form)

    @apply.autocomplete("form")
    async def _form_ac(self, interaction: discord.Interaction, current: str):
        rows = await self.bot.db.fetchall("SELECT id, name FROM application_forms WHERE guild_id=? AND enabled=1 ORDER BY name LIMIT 25", (interaction.guild_id,))
        return [app_commands.Choice(name=r["name"], value=r["id"]) for r in rows if current.lower() in r["name"].lower()]

    @app_commands.command(description="List pending applications")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def applications(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            """SELECT s.id, s.user_id, s.created_at, f.name FROM application_submissions s JOIN application_forms f ON f.id=s.form_id
               WHERE s.guild_id=? AND s.status='pending' ORDER BY s.id LIMIT 20""", (interaction.guild_id,))
        embed = brand_embed("Pending applications", "\n".join(f"`#{r['id']}` **{r['name']}** — <@{r['user_id']}> · {r['created_at'][:10]}" for r in rows) or "None 🎉")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Review on dashboard", url=f"{self.bot.config.public_url}/dashboard/{interaction.guild_id}/applications"))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Applications(bot))
