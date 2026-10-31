"""Bot behaviour tests. Real cogs, real discord.py models; only Discord network calls are intercepted."""
from datetime import timedelta

import discord
import pytest

from nexora.db import jdump
from tests.harness import CAT_TICKETS, CH_GENERAL, CH_LOGS, GUILD_ID, MOD_ID, NOBODY_ID, OWNER_ID, ROLE_ADMIN, ROLE_MEMBER, Env, run


class Net:
    """Records intercepted Discord API calls."""

    def __init__(self, monkeypatch, env_holder):
        self.calls: list[tuple] = []
        rec = self.calls
        self._id = 990000000000000000

        async def send(target, content=None, **kw):
            rec.append(("send", getattr(target, "id", None), content, kw.get("embed")))
            return _FakeMessage(self._next(), target)

        async def add_roles(member, *roles, **kw):
            rec.append(("add_roles", member.id, [r.id for r in roles]))

        async def remove_roles(member, *roles, **kw):
            rec.append(("remove_roles", member.id, [r.id for r in roles]))

        async def timeout(member, until, **kw):
            rec.append(("timeout", member.id, until))

        async def kick(member, **kw):
            rec.append(("kick", member.id))

        async def delete(obj, **kw):
            rec.append(("delete", obj.id))

        async def noop(*a, **kw):
            rec.append(("noop", a[0].__class__.__name__))

        async def create_text_channel(guild, name, **kw):
            rec.append(("create_channel", name, kw.get("category").id if kw.get("category") else None))
            return guild.get_channel(CH_LOGS)

        async def pm_edit(pm, **kw):
            rec.append(("edit_message", pm.id, kw.get("embed")))

        for cls in (discord.TextChannel, discord.Member, discord.User):
            monkeypatch.setattr(cls, "send", send)
        monkeypatch.setattr(discord.Member, "add_roles", add_roles)
        monkeypatch.setattr(discord.Member, "remove_roles", remove_roles)
        monkeypatch.setattr(discord.Member, "timeout", timeout)
        monkeypatch.setattr(discord.Member, "kick", kick)
        monkeypatch.setattr(discord.Message, "delete", delete)
        monkeypatch.setattr(discord.Message, "add_reaction", noop)
        monkeypatch.setattr(discord.TextChannel, "set_permissions", noop)
        monkeypatch.setattr(discord.TextChannel, "edit", noop)
        monkeypatch.setattr(discord.TextChannel, "delete", delete)
        monkeypatch.setattr(discord.Guild, "create_text_channel", create_text_channel)
        monkeypatch.setattr(discord.PartialMessage, "edit", pm_edit)

        def history(channel, **kw):
            async def gen():
                for m in env_holder.get("history", []):
                    yield m
            return gen()
        monkeypatch.setattr(discord.TextChannel, "history", history)

    def _next(self):
        self._id += 1
        return self._id

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class _FakeMessage:
    def __init__(self, mid, channel):
        self.id, self.channel = mid, channel
        self.jump_url = f"https://discord.com/channels/x/{getattr(channel, 'id', 0)}/{mid}"

    async def create_thread(self, **kw):
        return None


def make_message(env, author_id, content, channel_id=CH_GENERAL, mentions=None, attachments=None):
    state = env.bot._connection
    channel = env.guild.get_channel(channel_id)
    member = env.guild.get_member(author_id)
    data = {
        "id": str(970000000000000000 + len(content) + author_id % 1000), "channel_id": str(channel_id), "guild_id": str(GUILD_ID),
        "author": {"id": str(author_id), "username": member.name, "discriminator": "0", "avatar": None},
        "member": {"roles": [str(r.id) for r in member.roles if not r.is_default()], "joined_at": "2024-01-01T00:00:00+00:00", "deaf": False, "mute": False, "flags": 0},
        "content": content, "timestamp": "2024-06-01T00:00:00+00:00", "edited_timestamp": None, "tts": False,
        "mention_everyone": False, "mentions": mentions or [], "mention_roles": [], "attachments": attachments or [], "embeds": [],
        "pinned": False, "type": 0,
    }
    return discord.Message(state=state, channel=channel, data=data)


# ---------------------------------------------------------------- logging
def test_logging_posts_to_category_channel_and_respects_toggles(monkeypatch):
    holder = {}
    net = Net(monkeypatch, holder)

    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "logging", {"default_channel_id": CH_GENERAL, "moderation_channel_id": CH_LOGS, "voice_enabled": False})
            cog = env.bot.get_cog("Logging")
            await cog.record(env.guild, "moderation", "ban", "x banned", discord.Embed(title="Ban"))
            await cog.record(env.guild, "members", "member_join", "y joined", discord.Embed(title="Join"))
            await cog.record(env.guild, "voice", "voice_join", "z joined vc", discord.Embed(title="Voice"))
            sends = net.of("send")
            assert [(s[1], s[3].title) for s in sends] == [(CH_LOGS, "Ban"), (CH_GENERAL, "Join")]
            assert await env.db.fetchval("SELECT COUNT(*) FROM log_events") == 2
    run(t())


# ------------------------------------------------------------- moderation
def test_warning_escalation_times_out_member(monkeypatch):
    net = Net(monkeypatch, {})

    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "moderation", {"dm_users": False, "escalation": [{"warnings": 2, "action": "timeout", "duration_minutes": 30}]})
            mod = env.bot.get_cog("Moderation")
            member = env.guild.get_member(NOBODY_ID)
            _, n1, esc1 = await mod.issue_warning(env.guild, member, env.guild.get_member(MOD_ID), "one")
            _, n2, esc2 = await mod.issue_warning(env.guild, member, env.guild.get_member(MOD_ID), "two")
            assert (n1, esc1) == (1, None) and n2 == 2 and esc2 == "timed out for 30m"
            assert net.of("timeout")[0][:2] == ("timeout", NOBODY_ID) and net.of("timeout")[0][2] == timedelta(minutes=30)
            actions = [r["action"] for r in await env.db.fetchall("SELECT action FROM mod_cases ORDER BY case_number")]
            assert actions == ["warn", "warn", "timeout"]
            assert await env.db.fetchval("SELECT COUNT(*) FROM staff_activity WHERE user_id=?", (MOD_ID,)) == 1
    run(t())


def test_tempban_expiry_unbans(monkeypatch):
    Net(monkeypatch, {})
    unbanned = []

    async def unban(guild, user, **kw):
        unbanned.append(user.id)
    monkeypatch.setattr(discord.Guild, "unban", unban)

    async def t():
        async with Env(ready=True) as env:
            await env.db.execute("INSERT INTO mod_cases (guild_id, case_number, action, user_id, expires_at) VALUES (?, 1, 'tempban', 42, datetime('now','-1 minute'))", (GUILD_ID,))
            await env.bot.get_cog("Moderation").expiry_loop.coro(env.bot.get_cog("Moderation"))
            assert unbanned == [42]
            assert await env.db.fetchval("SELECT active FROM mod_cases WHERE case_number=1") == 0
            assert await env.db.fetchval("SELECT action FROM mod_cases WHERE case_number=2") == "unban"
    run(t())


# ---------------------------------------------------------------- automod
def test_automod_deletes_invite_and_warns(monkeypatch):
    net = Net(monkeypatch, {})

    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "automod", {"enabled": True, "invites_enabled": True, "invites_action": "delete_warn", "invites_allow_own": False})
            await env.bot.settings.update(GUILD_ID, "moderation", {"dm_users": False})
            cog = env.bot.get_cog("Automod")
            await cog.on_message(make_message(env, NOBODY_ID, "join discord.gg/raid"))
            assert net.of("delete") and await env.db.fetchval("SELECT COUNT(*) FROM warnings WHERE user_id=?", (NOBODY_ID,)) == 1
            flag = await env.db.fetchone("SELECT * FROM automod_flags")
            assert flag["rule"] == "invites" and "deleted" in flag["action_taken"] and flag["case_number"] == 1
            # staff (Manage Messages) are exempt by default
            net.calls.clear()
            await cog.on_message(make_message(env, MOD_ID, "discord.gg/fine"))
            assert not net.of("delete")
    run(t())


def test_automod_spam_and_flag_only(monkeypatch):
    net = Net(monkeypatch, {})

    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "automod", {"enabled": True, "spam_enabled": True, "spam_max_messages": 3, "spam_seconds": 10, "spam_action": "flag"})
            cog = env.bot.get_cog("Automod")
            for i in range(4):
                await cog.on_message(make_message(env, NOBODY_ID, f"msg {i}" + "x" * i))
            assert not net.of("delete") and not net.of("timeout")
            assert await env.db.fetchval("SELECT status FROM automod_flags") == "pending"
    run(t())


# --------------------------------------------------------------- leveling
def test_leveling_rewards_and_announcement(monkeypatch):
    net = Net(monkeypatch, {})

    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "leveling", {"enabled": True, "rewards": [{"level": 1, "role_id": ROLE_MEMBER}],
                                                                 "levelup_message": "GG {user} → {level}", "announce_channel_id": CH_GENERAL})
            lv = env.bot.get_cog("Leveling")
            new = await lv.add_xp(env.guild.get_member(MOD_ID), 120, None)
            assert new == 1
            assert ("add_roles", MOD_ID, [ROLE_MEMBER]) in net.calls
            assert any(c[0] == "send" and c[1] == CH_GENERAL and "→ 1" in c[2] for c in net.calls)
            # cooldown: a message right after doesn't give XP
            before = await env.db.fetchval("SELECT xp FROM levels WHERE user_id=?", (MOD_ID,))
            await lv.on_message(make_message(env, MOD_ID, "hello"))
            assert await env.db.fetchval("SELECT xp FROM levels WHERE user_id=?", (MOD_ID,)) == before
    run(t())


# ---------------------------------------------------------------- economy
def test_economy_balance_spend_achievements(monkeypatch):
    Net(monkeypatch, {})

    async def t():
        async with Env(ready=True) as env:
            eco = env.bot.get_cog("Economy")
            assert await eco.add_balance(GUILD_ID, NOBODY_ID, 10_500) == 10_500
            assert await eco.try_spend(GUILD_ID, NOBODY_ID, 10_000)
            assert not await eco.try_spend(GUILD_ID, NOBODY_ID, 10_000)
            assert await env.db.fetchval("SELECT balance FROM economy WHERE user_id=?", (NOBODY_ID,)) == 500
            assert await env.db.fetchval("SELECT key FROM achievements WHERE user_id=?", (NOBODY_ID,)) == "rich_10k"
    run(t())


# ----------------------------------------------------------------- welcome
def test_welcome_message_and_autoroles(monkeypatch):
    net = Net(monkeypatch, {})

    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "welcome", {"welcome_enabled": True, "welcome_channel_id": CH_GENERAL, "welcome_embed": False,
                                                                "welcome_message": "Hi {user.name}, member #{member_count}", "autorole_ids": [ROLE_MEMBER]})
            member = env.guild.get_member(MOD_ID)
            await env.bot.get_cog("Welcome").on_member_join(member)
            import asyncio
            await asyncio.sleep(0.05)
            assert ("send", CH_GENERAL, "Hi moddy, member #5", None) in net.calls
            assert ("add_roles", MOD_ID, [ROLE_MEMBER]) in net.calls
    run(t())


# ------------------------------------------------------- community / utility
def test_counting_and_custom_commands(monkeypatch):
    net = Net(monkeypatch, {})

    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "counting", {"enabled": True, "channel_id": CH_LOGS})
            comm = env.bot.get_cog("Community")
            await comm.on_message(make_message(env, NOBODY_ID, "1", CH_LOGS))
            await comm.on_message(make_message(env, MOD_ID, "2", CH_LOGS))
            assert await env.db.fetchval("SELECT current FROM counting_state") == 2
            await comm.on_message(make_message(env, MOD_ID, "3", CH_LOGS))  # same user twice → reset
            assert await env.db.fetchval("SELECT current FROM counting_state") == 0
            assert await env.db.fetchval("SELECT high_score FROM counting_state") == 2

            await env.db.execute("INSERT INTO custom_commands (guild_id, name, response) VALUES (?, 'rules', 'Be kind, {user.name}! {args}')", (GUILD_ID,))
            net.calls.clear()
            await env.bot.get_cog("Utility").on_message(make_message(env, NOBODY_ID, "!rules please"))
            assert net.of("send")[0][2] == "Be kind, nobody! please"
            assert await env.db.fetchval("SELECT uses FROM custom_commands") == 1
    run(t())


# ----------------------------------------------------------------- tickets
def test_ticket_open_and_close_flow(monkeypatch):
    holder = {}
    net = Net(monkeypatch, holder)

    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "tickets", {"transcript_channel_id": CH_GENERAL, "delete_after_close_seconds": 0, "ratings_enabled": True})
            pid = await env.db.execute("INSERT INTO ticket_panels (guild_id, name, category_id, staff_role_ids) VALUES (?, 'Support', ?, ?)",
                                       (GUILD_ID, CAT_TICKETS, jdump([ROLE_ADMIN])))
            panel = await env.db.fetchone("SELECT * FROM ticket_panels WHERE id=?", (pid,))
            tickets = env.bot.get_cog("Tickets")
            await tickets.create_ticket(env.guild.get_member(NOBODY_ID), panel, [{"question": "Issue", "answer": "<b>help</b>"}])
            assert net.of("create_channel")[0] == ("create_channel", "ticket-0001", CAT_TICKETS)
            t_row = await env.db.fetchone("SELECT * FROM tickets")
            assert t_row["number"] == 1 and t_row["channel_id"] == CH_LOGS and t_row["status"] == "open"
            assert await tickets.is_staff(env.guild.get_member(MOD_ID), t_row)
            assert not await tickets.is_staff(env.guild.get_member(NOBODY_ID), t_row)
            holder["history"] = [make_message(env, NOBODY_ID, "my <i>problem</i>", CH_LOGS)]
            net.calls.clear()
            await tickets.close(t_row, env.guild.get_member(MOD_ID), "solved")
            t_row = await env.db.fetchone("SELECT * FROM tickets")
            assert t_row["status"] == "closed" and t_row["close_reason"] == "solved" and "&lt;i&gt;problem" in t_row["transcript"]
            targets = [c[1] for c in net.of("send")]
            assert CH_GENERAL in targets and NOBODY_ID in targets  # transcript channel + DM with rating
            assert "already closed" in await tickets.close(t_row, None, None)
    run(t())


# -------------------------------------------------------------- giveaways
def test_giveaway_end_announces_and_reroll(monkeypatch):
    net = Net(monkeypatch, {})

    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "giveaways", {"dm_winners": False})
            gid = await env.db.execute("INSERT INTO giveaways (guild_id, channel_id, message_id, prize, winner_count, starts_at, ends_at, status) VALUES (?, ?, 1, 'Nitro', 1, datetime('now'), datetime('now','-1 second'), 'running')",
                                       (GUILD_ID, CH_GENERAL))
            for uid in (NOBODY_ID, MOD_ID):
                await env.db.execute("INSERT INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)", (gid, uid))
            cog = env.bot.get_cog("Giveaways")
            await cog.loop.coro(cog)  # the scheduler loop ends due giveaways
            g = await env.db.fetchone("SELECT * FROM giveaways")
            first = __import__("json").loads(g["winners"])
            assert g["status"] == "ended" and len(first) == 1 and first[0] in (NOBODY_ID, MOD_ID)
            assert any("You won **Nitro**" in (c[2] or "") for c in net.of("send"))
            second = await cog.reroll(gid, 1)
            assert second and second[0] != first[0]
            assert await cog.reroll(gid, 1) == []  # nobody left
    run(t())


@pytest.mark.parametrize("mod_name", ["Core", "Logging", "Moderation", "Automod", "Tickets", "Applications", "Giveaways", "Welcome",
                                      "Roles", "Leveling", "Economy", "Community", "Utility", "Automation", "AI"])
def test_all_cogs_load(mod_name):
    async def t():
        async with Env(ready=False) as env:
            assert env.bot.get_cog(mod_name) is not None
    run(t())


def test_help_lists_only_real_commands():
    from nexora.bot.cogs.core import HELP_CATEGORIES

    async def t():
        async with Env(ready=False) as env:
            real = {c.qualified_name for c in env.bot.tree.walk_commands()}
            listed = {c.strip() for v in HELP_CATEGORIES.values() for c in v.split(",")}
            assert listed <= real, listed - real
            assert len([c for c in env.bot.tree.get_commands()]) <= 100  # Discord's global command limit
    run(t())
