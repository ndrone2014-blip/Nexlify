"""End-to-end tests of the website and dashboard API against a real server and real discord.py objects."""
import json

from nexora.db import jdump
from nexora.web.security import RateLimiter
from tests.harness import (BOT_ID, CAT_TICKETS, CH_GENERAL, CH_LOGS, GUILD_ID, MOD_ID, NOBODY_ID, OWNER_ID, READER_ID, ROLE_ADMIN, ROLE_BOT,
                           ROLE_MEMBER, ROLE_READONLY, Env, run)


async def _readonly_role(env):
    await env.bot.settings.update(GUILD_ID, "general", {"dashboard_readonly_role_ids": [ROLE_READONLY]})


# ------------------------------------------------------------------ public
def test_public_pages_and_headers():
    async def t():
        async with Env(ready=True) as env:
            for path in ("/", "/features", "/commands", "/docs", "/support", "/status"):
                status, body, headers = await env.req("GET", path, user=None, raw=True)
                assert status == 200, path
                assert headers["X-Frame-Options"] == "DENY" and "default-src 'self'" in headers["Content-Security-Policy"]
            status, body, _ = await env.req("GET", "/commands", user=None, raw=True)
            assert "/ticket close" in body and "/giveaway start" in body and "Requires Ban Members" in body
            status, data = await env.req("GET", "/api/status", user=None)
            assert data["bot"] is True and data["database"] is True
            status, _, headers = await env.req("GET", "/invite", user=None, raw=True)
            assert status in (302, 307) and "client_id=123" in headers["Location"]
            assert (await env.req("GET", "/nope", user=None, raw=True))[0] == 404
    run(t())


def test_auth_required_and_redirects():
    async def t():
        async with Env(ready=True) as env:
            assert (await env.req("GET", "guild/meta", user=None))[0] == 401
            status, _, headers = await env.req("GET", f"/dashboard/{GUILD_ID}", user=None, raw=True)
            assert status in (302, 307) and headers["Location"].startswith("/auth/login")
            status, _, headers = await env.req("GET", "/auth/login?next=//evil.com", user=None, raw=True)
            assert "discord.com/oauth2/authorize" in headers["Location"] and "nx_oauth" in headers.get("Set-Cookie", "")
            assert (await env.req("GET", "/auth/callback?code=x&state=y", user=None))[0] == 400  # no signed state cookie
            status, _, _ = await env.req("GET", f"/dashboard/{GUILD_ID}", raw=True)
            assert status == 200
    run(t())


def test_access_control_and_csrf():
    async def t():
        async with Env(ready=True) as env:
            await _readonly_role(env)
            assert (await env.req("GET", "guild/meta", user=NOBODY_ID))[0] == 403
            assert (await env.req("GET", "/api/guilds/123/meta"))[0] == 404
            s, d = await env.req("GET", "guild/meta", user=READER_ID)
            assert s == 200 and d["access"] == "read"
            assert (await env.req("PUT", "guild/settings/leveling", user=READER_ID, json={"enabled": True}))[0] == 403
            s, d = await env.req("GET", "guild/meta", user=MOD_ID)
            assert d["access"] == "manage"
            assert (await env.req("PUT", "guild/settings/leveling", user=MOD_ID, json={"enabled": True}, csrf=False))[0] == 403
            assert (await env.req("PUT", "guild/settings/leveling", user=MOD_ID, json={"enabled": True}))[0] == 200
            # server list hides servers the user can't manage
            _, d = await env.req("GET", "/api/guilds", user=NOBODY_ID)
            assert d["guilds"] == []
            # extra dashboard role grants manage access
            await env.bot.settings.update(GUILD_ID, "general", {"dashboard_role_ids": [ROLE_MEMBER]})
            assert (await env.req("GET", "guild/meta", user=NOBODY_ID))[1]["access"] == "manage"
            # logout needs the CSRF token
            assert (await env.req("POST", "/auth/logout", user=MOD_ID, csrf=False))[0] == 403
            s, _, _ = await env.req("POST", "/auth/logout", user=MOD_ID, raw=True)
            assert s == 303
            assert (await env.req("GET", "guild/meta", user=MOD_ID))[0] == 401
    run(t())


def test_snowflakes_are_strings():
    async def t():
        async with Env(ready=True) as env:
            _, d = await env.req("GET", "guild/meta")
            assert d["guild"]["id"] == str(GUILD_ID)
            assert all(isinstance(c["id"], str) for c in d["channels"])
            await env.req("PUT", "guild/settings/logging", json={"default_channel_id": str(CH_LOGS)})
            _, d = await env.req("GET", "guild/settings/logging")
            assert d["values"]["default_channel_id"] == str(CH_LOGS)
    run(t())


# ---------------------------------------------------------------- settings
def test_settings_roundtrip_validation_reset_and_audit():
    async def t():
        async with Env(ready=True) as env:
            s, d = await env.req("PUT", "guild/settings/welcome", json={"welcome_enabled": True, "welcome_channel_id": str(CH_GENERAL),
                                                                         "autorole_ids": [str(ROLE_MEMBER)], "welcome_message": "Hi {user}"})
            assert s == 200 and d["values"]["welcome_enabled"] is True
            live = await env.bot.settings.get(GUILD_ID, "welcome")  # the bot sees the change immediately
            assert live["welcome_channel_id"] == CH_GENERAL and live["autorole_ids"] == [ROLE_MEMBER]
            s, d = await env.req("PUT", "guild/settings/welcome", json={"autorole_delay_seconds": 999999, "nope": 1})
            assert s == 422 and set(d["errors"]) == {"autorole_delay_seconds", "nope"}
            assert (await env.req("PUT", "guild/settings/unknown", json={}))[0] == 422
            s, d = await env.req("POST", "guild/settings/welcome/reset")
            assert d["values"]["welcome_enabled"] is False
            _, d = await env.req("GET", "guild/audit")
            actions = [e["action"] for e in d["entries"]]
            assert "settings.update:welcome" in actions and "settings.reset:welcome" in actions
    run(t())


# ------------------------------------------------------------------- logs
def test_logs_search_filter_export():
    async def t():
        async with Env(ready=True) as env:
            logging_cog = env.bot.get_cog("Logging")
            for i in range(60):
                await logging_cog.record(env.guild, "messages", "message_edit", f"edit number {i}", None, user_id=NOBODY_ID,
                                         channel_id=CH_GENERAL, data={"before": f"old {i}", "after": f"new {i}"})
            await logging_cog.record(env.guild, "moderation", "ban", "someone banned", None, user_id=READER_ID, moderator_id=MOD_ID)
            await env.bot.settings.update(GUILD_ID, "logging", {"voice_enabled": False, "ignored_channel_ids": [CH_LOGS]})
            await logging_cog.record(env.guild, "voice", "voice_join", "should not be stored", None)
            await logging_cog.record(env.guild, "messages", "message_delete", "ignored channel", None, channel_id=CH_LOGS)
            _, d = await env.req("GET", "guild/logs?limit=50")
            assert d["total"] == 61 and len(d["events"]) == 50 and d["next_before_id"]
            assert d["events"][0]["summary"] == "someone banned" and d["events"][0]["moderator"]["name"] == "Moddy"
            _, d2 = await env.req("GET", f"guild/logs?limit=50&before_id={d['next_before_id']}")
            assert len(d2["events"]) == 11
            _, d = await env.req("GET", "guild/logs?category=moderation")
            assert d["total"] == 1
            _, d = await env.req("GET", "guild/logs?q=number 42")
            assert d["total"] == 1 and d["events"][0]["data"]["before"] == "old 42"
            _, d = await env.req("GET", f"guild/logs?user_id={NOBODY_ID}&event_type=message_edit")
            assert d["total"] == 60
            assert (await env.req("GET", "guild/logs?user_id=abc"))[0] == 400
            s, body, h = await env.req("GET", "guild/logs/export?format=csv&category=moderation", raw=True)
            assert s == 200 and "someone banned" in body and "text/csv" in h["Content-Type"]
            s, body, _ = await env.req("GET", "guild/logs/export?format=json", raw=True)
            rows = json.loads(body)
            assert len(rows) == 61 and rows[0]["user_id"] == str(READER_ID)
    run(t())


# -------------------------------------------------------------- moderation
def test_moderation_actions_cases_warnings_notes():
    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "moderation", {"dm_users": False, "escalation_enabled": False})
            s, d = await env.req("POST", "guild/moderation/action", user=MOD_ID, json={"action": "warn", "user_id": str(NOBODY_ID), "reason": "spam"})
            assert s == 200 and "case #1" in d["message"], d
            await env.req("POST", "guild/moderation/action", user=MOD_ID, json={"action": "warn", "user_id": str(NOBODY_ID), "reason": "again"})
            # hierarchy: a moderator can't act on the server owner
            s, d = await env.req("POST", "guild/moderation/action", user=MOD_ID, json={"action": "kick", "user_id": str(OWNER_ID)})
            assert s == 403
            # read-only users can't act
            await _readonly_role(env)
            assert (await env.req("POST", "guild/moderation/action", user=READER_ID, json={"action": "warn", "user_id": str(NOBODY_ID)}))[0] == 403
            assert (await env.req("POST", "guild/moderation/action", user=MOD_ID, json={"action": "explode", "user_id": "1"}))[0] == 400
            _, d = await env.req("GET", "guild/cases")
            assert [c["case_number"] for c in d["cases"]] == [2, 1] and d["moderators"][0]["actions"] == 2
            assert (await env.req("PATCH", "guild/cases/1", json={"reason": "edited"}))[0] == 200
            _, d = await env.req("GET", f"guild/warnings?user_id={NOBODY_ID}")
            assert len(d["warnings"]) == 2 and {w["reason"] for w in d["warnings"]} == {"edited", "again"}
            assert (await env.req("DELETE", f"guild/warnings/{d['warnings'][0]['id']}"))[0] == 200
            assert (await env.req("POST", "guild/notes", json={"user_id": str(NOBODY_ID), "note": "watch them"}))[0] == 200
            _, p = await env.req("GET", f"guild/users/{NOBODY_ID}")
            assert len(p["cases"]) == 2 and p["notes"][0]["note"] == "watch them" and sum(w["active"] for w in p["warnings"]) == 1
            _, d = await env.req("GET", "guild/members/search?q=nob")
            assert d["members"][0]["id"] == str(NOBODY_ID)
            # the moderation log category recorded the cases
            _, d = await env.req("GET", "guild/logs?category=moderation")
            assert d["total"] >= 2
    run(t())


def test_require_reason_setting():
    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "moderation", {"require_reason": True, "dm_users": False})
            s, d = await env.req("POST", "guild/moderation/action", json={"action": "warn", "user_id": str(NOBODY_ID)})
            assert s == 400 and "reason" in d["error"]
    run(t())


def test_appeals_and_automod_review():
    async def t():
        async with Env(ready=True) as env:
            db = env.db
            await db.execute("INSERT INTO mod_cases (guild_id, case_number, action, user_id, reason) VALUES (?, 1, 'warn', ?, 'x')", (GUILD_ID, NOBODY_ID))
            await db.execute("INSERT INTO warnings (guild_id, user_id, reason, case_number) VALUES (?, ?, 'x', 1)", (GUILD_ID, NOBODY_ID))
            # automod false positive voids the warning
            fid = await db.execute("INSERT INTO automod_flags (guild_id, user_id, rule, content, action_taken, case_number, status) VALUES (?, ?, 'caps', 'HELLO', 'deleted, warned (1)', 1, 'actioned')", (GUILD_ID, NOBODY_ID))
            _, d = await env.req("GET", "guild/automod/flags?status=actioned")
            assert d["flags"][0]["id"] == fid
            s, d = await env.req("POST", f"guild/automod/flags/{fid}/review", json={"correct": False})
            assert s == 200 and "voided" in d["message"]
            assert await db.fetchval("SELECT active FROM warnings WHERE case_number=1") == 0
            _, d = await env.req("POST", f"guild/automod/flags/{fid}/review", json={"correct": True})
            assert "Already" in d["message"]
            # appeal for a user the bot can't DM (unknown) is denied cleanly
            aid = await db.execute("INSERT INTO appeals (guild_id, user_id, case_number, content) VALUES (?, 5, 1, 'please')", (GUILD_ID,))
            _, d = await env.req("GET", "guild/appeals")
            assert d["appeals"][0]["id"] == aid
            s, d = await env.req("POST", f"guild/appeals/{aid}/resolve", json={"accept": False, "response": "no"})
            assert s == 200 and await db.fetchval("SELECT status FROM appeals WHERE id=?", (aid,)) == "denied"
    run(t())


# ----------------------------------------------------------------- tickets
def test_ticket_panels_crud_and_tickets():
    async def t():
        async with Env(ready=False) as env:
            body = {"name": "Support", "channel_id": str(CH_GENERAL), "category_id": str(CAT_TICKETS), "staff_role_ids": [str(ROLE_ADMIN)],
                    "questions": [{"label": "What's wrong?", "type": "long"}], "color": "#FF00FF"}
            s, d = await env.req("POST", "guild/ticket-panels", json=body)
            assert s == 200
            pid = d["id"]
            _, d = await env.req("GET", "guild/ticket-panels")
            p = d["panels"][0]
            assert p["staff_role_ids"] == [str(ROLE_ADMIN)] and p["color"] == "#FF00FF" and p["questions"][0]["long"] is True
            assert (await env.req("POST", "guild/ticket-panels", json={**body, "channel_id": "123"}))[0] == 422
            assert (await env.req("POST", "guild/ticket-panels", json={**body, "questions": [{"label": "q"}] * 6}))[0] == 422
            assert (await env.req("PUT", f"guild/ticket-panels/{pid}", json={**body, "name": "Help"}))[0] == 200
            assert (await env.req("POST", f"guild/ticket-panels/{pid}/publish"))[0] == 409  # bot offline → clear error
            tid = await env.db.execute("INSERT INTO tickets (guild_id, panel_id, number, opener_id, status, rating, transcript, closed_at) VALUES (?, ?, 1, ?, 'closed', 5, '<html>t<script>x</script></html>', datetime('now'))",
                                       (GUILD_ID, pid, NOBODY_ID))
            _, d = await env.req("GET", "guild/tickets?status=all")
            assert d["stats"]["avg_rating"] == 5 and d["tickets"][0]["panel"] == "Help" and d["tickets"][0]["has_transcript"] == 1
            s, body_html, h = await env.req("GET", f"/dashboard/{GUILD_ID}/tickets/{tid}/transcript", raw=True)
            assert s == 200 and "sandbox" in h["Content-Security-Policy"]
            assert (await env.req("DELETE", f"guild/ticket-panels/{pid}"))[0] == 200
            assert (await env.req("GET", f"/dashboard/{GUILD_ID}/tickets/999/transcript", raw=True))[0] == 404
    run(t())


def test_transcript_escapes_html():
    from types import SimpleNamespace
    from nexora.bot.cogs.tickets import build_transcript
    author = SimpleNamespace(display_avatar=SimpleNamespace(url="https://a/b.png"), __str__=lambda s: "x")
    msg = SimpleNamespace(content="<script>alert(1)</script>", embeds=[], attachments=[], author=author,
                          created_at=__import__("datetime").datetime(2024, 1, 1))
    html = build_transcript("G<b>", "c", {"number": 1, "opener_id": 1}, [msg])
    assert "<script>alert" not in html and "&lt;script&gt;" in html and "G&lt;b&gt;" in html


# ------------------------------------------------------------ applications
def test_application_forms_and_review_flow():
    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "applications", {"dm_on_decision": False})
            form = {"name": "Mod app", "kind": "moderator", "questions": [{"label": "Age?", "type": "short"}, {"label": "Timezone", "type": "choice", "options": ["EU", "US"]}],
                    "reviewer_role_ids": [str(ROLE_ADMIN)], "required_votes": 2}
            s, d = await env.req("POST", "guild/application-forms", json=form)
            assert s == 200
            fid = d["id"]
            assert (await env.req("POST", "guild/application-forms", json={**form, "questions": [{"label": "x", "type": "choice", "options": ["one"]}]}))[0] == 422
            assert (await env.req("POST", "guild/application-forms", json={**form, "kind": "wizard"}))[0] == 422
            sid = await env.db.execute("INSERT INTO application_submissions (guild_id, form_id, user_id, answers) VALUES (?, ?, ?, ?)",
                                       (GUILD_ID, fid, NOBODY_ID, jdump([{"question": "Age?", "answer": "20"}])))
            _, d = await env.req("GET", "guild/applications?status=pending")
            assert d["submissions"][0]["id"] == sid and d["stats"]["pending"] == 1
            s, d = await env.req("POST", f"guild/applications/{sid}/vote", user=MOD_ID, json={"accept": True})
            assert "1 accept" in d["message"]  # needs 2 votes
            assert (await env.req("POST", f"guild/applications/{sid}/notes", user=MOD_ID, json={"note": "good"}))[0] == 200
            # reader isn't a reviewer
            await _readonly_role(env)
            assert (await env.req("POST", f"guild/applications/{sid}/vote", user=READER_ID, json={"accept": True}))[0] == 403
            s, d = await env.req("POST", f"guild/applications/{sid}/vote", user=OWNER_ID, json={"accept": True})
            assert "accepted" in d["message"].lower()
            _, d = await env.req("GET", f"guild/applications/{sid}")
            assert d["status"] == "accepted" and len(d["votes"]) == 2 and d["notes"][0]["note"] == "good"
            _, d = await env.req("GET", "guild/application-forms")
            assert d["forms"][0]["accepted"] == 1 and d["forms"][0]["questions"][1]["options"] == ["EU", "US"]
            # eligibility: cooldown & limits use the real cog logic
            cog = env.bot.get_cog("Applications")
            f = await env.db.fetchone("SELECT * FROM application_forms WHERE id=?", (fid,))
            assert "apply again" in await cog.eligibility(GUILD_ID, NOBODY_ID, f)
            assert await cog.eligibility(GUILD_ID, MOD_ID, f) is None
    run(t())


# --------------------------------------------------------------- giveaways
def test_giveaways_need_bot_and_draw_verifies_members():
    async def t():
        async with Env(ready=False) as env:
            s, d = await env.req("POST", "guild/giveaways", json={"prize": "Nitro", "channel_id": str(CH_GENERAL), "duration": "1d"})
            assert s == 409
            assert (await env.req("POST", "guild/giveaways", json={"prize": "Nitro", "channel_id": str(CH_GENERAL), "duration": "5s"}))[0] == 422
            gid = await env.db.execute("INSERT INTO giveaways (guild_id, channel_id, prize, winner_count, starts_at, ends_at, status, required_role_ids) VALUES (?, ?, 'Nitro', 5, datetime('now'), datetime('now'), 'running', ?)",
                                       (GUILD_ID, CH_GENERAL, jdump([str(ROLE_MEMBER)])))
            for uid in (NOBODY_ID, MOD_ID, 12345, BOT_ID):
                await env.db.execute("INSERT INTO giveaway_entries (giveaway_id, user_id, entries) VALUES (?, ?, 1)", (gid, uid))
            g = await env.db.fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
            winners = await env.bot.get_cog("Giveaways").draw(g, 5, set())
            assert winners == [NOBODY_ID]  # MOD lacks the required role, 12345 left, bots excluded
            _, d = await env.req("GET", "guild/giveaways")
            assert d["giveaways"][0]["entry_count"] == 4 and d["stats"]["running"] == 1
    run(t())


# -------------------------------------------------------------- role menus
def test_role_menus_validation():
    async def t():
        async with Env(ready=True) as env:
            ok = {"kind": "button", "channel_id": str(CH_GENERAL), "title": "Roles", "options": [{"role_id": str(ROLE_MEMBER), "label": "Member"}]}
            s, d = await env.req("POST", "guild/role-menus", json=ok)
            assert s == 200
            s, d = await env.req("POST", "guild/role-menus", json={**ok, "options": [{"role_id": str(ROLE_BOT)}]})
            assert s == 422 and "can't assign" in d["error"]
            assert (await env.req("POST", "guild/role-menus", json={**ok, "kind": "reaction"}))[0] == 422  # needs emojis
            assert (await env.req("POST", "guild/role-menus", json={**ok, "options": []}))[0] == 422
            _, d = await env.req("GET", "guild/role-menus")
            assert d["menus"][0]["options"][0]["role_id"] == str(ROLE_MEMBER)
    run(t())


# ------------------------------------------------------------- collections
def test_collections_crud_and_validation():
    async def t():
        async with Env(ready=True) as env:
            s, d = await env.req("POST", "guild/collections/shop", json={"name": "VIP", "price": 500, "role_id": str(ROLE_MEMBER)})
            assert s == 200
            assert (await env.req("POST", "guild/collections/shop", json={"name": "Bad", "price": -1}))[0] == 422
            s, d = await env.req("POST", "guild/collections/commands", json={"name": "!Rules", "response": "Be nice, {user}"})
            assert s == 200
            s, d = await env.req("POST", "guild/collections/commands", json={"name": "rules", "response": "dup"})
            assert s == 422 and "already exists" in d["error"]
            assert (await env.req("POST", "guild/collections/commands", json={"name": "bad name", "response": "x"}))[0] == 422
            assert (await env.req("POST", "guild/collections/commands", json={"name": "empty"}))[0] == 422
            _, d = await env.req("GET", "guild/collections/commands")
            assert d["items"][0]["name"] == "rules"
            s, _ = await env.req("POST", "guild/collections/scheduled", json={"channel_id": str(CH_GENERAL), "content": "Hello", "next_run_at": "2030-01-01T10:00:00Z", "repeat_hours": 24})
            assert s == 200
            assert (await env.req("POST", "guild/collections/scheduled", json={"channel_id": str(CH_GENERAL), "content": "x", "next_run_at": "tomorrow"}))[0] == 422
            emb = {"title": "Hi", "description": "There", "color": "#112233", "fields": [{"name": "a", "value": "b"}], "buttons": [{"label": "Site", "url": "https://x.y"}]}
            s, d = await env.req("POST", "guild/collections/embeds", json={"name": "welcome", "data": emb})
            assert s == 200
            eid = d["id"]
            assert (await env.req("POST", "guild/collections/embeds", json={"name": "big", "data": {"description": "x" * 4096, "fields": [{"name": "n", "value": "v" * 1024}] * 2}}))[0] == 422
            assert (await env.req("POST", "guild/collections/embeds", json={"name": "badurl", "data": {"title": "t", "image": "javascript:x"}}))[0] == 422
            _, d = await env.req("GET", "guild/collections/embeds")
            assert d["items"][0]["data"]["buttons"][0]["url"] == "https://x.y"
            assert (await env.req("PUT", f"guild/collections/embeds/{eid}", json={"name": "welcome2", "data": emb}))[0] == 200
            assert (await env.req("DELETE", f"guild/collections/embeds/{eid}"))[0] == 200
            assert (await env.req("DELETE", f"guild/collections/embeds/{eid}"))[0] == 404
            assert (await env.req("GET", "guild/collections/secrets"))[0] == 404
    run(t())


# -------------------------------------------------------------- automation
def test_automations_crud_and_runtime():
    async def t():
        async with Env(ready=True) as env:
            await env.bot.settings.update(GUILD_ID, "leveling", {"enabled": True, "announce": False})
            body = {"name": "Join XP", "trigger": "member_join", "conditions": [{"type": "lacks_role", "value": str(ROLE_ADMIN)}],
                    "actions": [{"type": "add_xp", "amount": 150}, {"type": "give_currency", "amount": 25}]}
            s, d = await env.req("POST", "guild/automations", json=body)
            assert s == 200
            aid = d["id"]
            assert (await env.req("POST", "guild/automations", json={**body, "actions": [{"type": "add_role", "role": "1"}]}))[0] == 422
            cog = env.bot.get_cog("Automation")
            await cog.on_nexora_trigger(env.guild, "member_join", {"member": env.guild.get_member(NOBODY_ID)})
            await cog.on_nexora_trigger(env.guild, "member_join", {"member": env.guild.get_member(MOD_ID)})  # condition fails
            assert await env.db.fetchval("SELECT xp FROM levels WHERE user_id=?", (NOBODY_ID,)) == 150
            assert await env.db.fetchval("SELECT level FROM levels WHERE user_id=?", (NOBODY_ID,)) == 1
            assert await env.db.fetchval("SELECT balance FROM economy WHERE user_id=?", (NOBODY_ID,)) == 25
            _, d = await env.req("GET", f"guild/automations/{aid}/runs")
            assert [r["status"] for r in d["runs"]] == ["skipped", "ok"]
            assert (await env.req("POST", f"guild/automations/{aid}/toggle"))[0] == 200
            await cog.on_nexora_trigger(env.guild, "member_join", {"member": env.guild.get_member(NOBODY_ID)})
            assert await env.db.fetchval("SELECT xp FROM levels WHERE user_id=?", (NOBODY_ID,)) == 150  # disabled
            _, d = await env.req("GET", "guild/automations")
            assert d["automations"][0]["enabled"] == 0 and d["automations"][0]["runs"] == 1
            assert (await env.req("DELETE", f"guild/automations/{aid}"))[0] == 200
    run(t())


# ----------------------------------------------------- analytics & overview
def test_analytics_overview_and_export():
    async def t():
        async with Env(ready=True) as env:
            from datetime import date, timedelta
            today = date.today()
            for i in range(10):
                d = (today - timedelta(days=i)).isoformat()
                await env.db.execute("INSERT OR REPLACE INTO stats_daily (guild_id, day, joins, leaves, messages, member_count) VALUES (?, ?, 3, 1, 100, ?)", (GUILD_ID, d, 50 + i))
            _, d = await env.req("GET", "guild/analytics?days=7")
            assert len(d["days"]) == 7 and d["totals"]["joins"] == 21 and d["totals"]["net_growth"] == 14 and d["series"]["member_count"][-1] == 50
            _, d = await env.req("GET", f"guild/analytics?start={(today - timedelta(days=29)).isoformat()}&end={today.isoformat()}")
            assert d["totals"]["messages"] == 1000
            assert (await env.req("GET", "guild/analytics?start=2024-05-01&end=2024-01-01"))[0] == 400
            s, body, _ = await env.req("GET", "guild/analytics/export?days=7", raw=True)
            assert s == 200 and body.splitlines()[0].startswith("day,joins")
            _, o = await env.req("GET", "guild/overview")
            assert o["week"]["joins"] == 21 and o["members"]["total"] == 5
    run(t())


# ------------------------------------------------------- backup & system
def test_config_export_import_roundtrip():
    async def t():
        async with Env(ready=True) as env:
            await env.req("PUT", "guild/settings/economy", json={"enabled": True, "daily_amount": 777})
            await env.req("POST", "guild/collections/commands", json={"name": "hello", "response": "hi"})
            s, body, _ = await env.req("GET", "guild/config/export", raw=True)
            backup = json.loads(body)
            assert backup["modules"]["economy"]["daily_amount"] == 777
            await env.req("POST", "guild/settings/economy/reset")
            backup["replace"] = True
            s, d = await env.req("POST", "guild/config/import", json=backup)
            assert s == 200 and d["rows"]["custom_commands"] == 1
            assert (await env.bot.settings.get(GUILD_ID, "economy"))["daily_amount"] == 777
            assert await env.db.fetchval("SELECT COUNT(*) FROM custom_commands") == 1
            assert (await env.req("POST", "guild/config/import", json={"hello": 1}))[0] == 400
    run(t())


def test_system_owner_only_backup_restore():
    async def t():
        async with Env(ready=True) as env:
            assert (await env.req("GET", "/api/system", user=MOD_ID))[0] == 403
            s, d = await env.req("GET", "/api/system")
            assert s == 200 and "Moderation" in d["bot"]["cogs"]
            await env.bot.settings.update(GUILD_ID, "economy", {"daily_amount": 111})
            _, d = await env.req("POST", "/api/system/backup")
            name = d["name"]
            await env.bot.settings.update(GUILD_ID, "economy", {"daily_amount": 222})
            s, d = await env.req("POST", "/api/system/restore", json={"name": name})
            assert s == 200 and d["safety_backup"]
            assert (await env.bot.settings.get(GUILD_ID, "economy"))["daily_amount"] == 111
            assert (await env.req("POST", "/api/system/restore", json={"name": "../t.db"}))[0] == 404
            assert (await env.req("GET", "/system", user=MOD_ID, raw=True))[0] == 403
    run(t())


def test_public_leaderboard_respects_module_toggle():
    async def t():
        async with Env(ready=True) as env:
            await env.db.execute("INSERT INTO levels (guild_id, user_id, xp, level, messages) VALUES (?, ?, 500, 3, 40)", (GUILD_ID, NOBODY_ID))
            assert (await env.req("GET", f"/leaderboard/{GUILD_ID}", user=None, raw=True))[0] == 404
            await env.bot.settings.update(GUILD_ID, "leveling", {"enabled": True})
            s, body, _ = await env.req("GET", f"/leaderboard/{GUILD_ID}", user=None, raw=True)
            assert s == 200 and "Nobody" in body
    run(t())


def test_rate_limiter():
    rl = RateLimiter()
    assert all(rl.allow("k", 3, 60) for _ in range(3))
    assert not rl.allow("k", 3, 60)
    assert rl.allow("other", 3, 60)


def test_dashboard_access_without_member_cache(monkeypatch):
    """With the Server Members intent off, members aren't cached; access is checked via Discord's API instead."""
    import discord

    async def t():
        async with Env(ready=True) as env:
            owner = env.guild.get_member(OWNER_ID)
            env.guild._members.pop(OWNER_ID)  # simulate an empty member cache

            async def fetch_member(guild, uid):
                if uid == OWNER_ID:
                    return owner
                raise discord.NotFound(type("R", (), {"status": 404, "reason": "x"})(), "Unknown Member")
            monkeypatch.setattr(discord.Guild, "fetch_member", fetch_member)
            _, d = await env.req("GET", "/api/guilds")
            assert d["guilds"][0]["bot_in"] is True and d["guilds"][0]["access"] == "manage"
            assert (await env.req("GET", "guild/meta"))[0] == 200
            env.guild._members.pop(NOBODY_ID)
            assert (await env.req("GET", "guild/meta", user=NOBODY_ID))[0] == 403
    run(t())


def test_server_list_is_rendered_without_javascript():
    """The /dashboard page lists servers in the HTML itself, and static JS is served with a JS MIME type."""
    async def t():
        async with Env(ready=True) as env:
            status, html, _ = await env.req("GET", "/dashboard", raw=True)
            assert status == 200
            assert "Test Server" in html and f'href="/dashboard/{GUILD_ID}"' in html
            status, html, _ = await env.req("GET", "/dashboard", user=NOBODY_ID, raw=True)
            assert status == 200 and "Test Server" not in html and "don't manage any servers" in html
            status, _, headers = await env.req("GET", "/static/js/public.js", user=None, raw=True)
            assert status == 200 and headers["content-type"].startswith("text/javascript")
            assert headers["cache-control"] == "no-cache"
    run(t())
