"""Core dashboard API: identity, server list, metadata, settings, overview, analytics, audit, config backup, system."""
from __future__ import annotations

import csv
import io
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import discord
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from nexora.core import schema
from nexora.core.automation_engine import ACTIONS, CONDITIONS, TRIGGERS
from nexora.core.permissions import settings_problems
from nexora.db import jdump, jload
from nexora.web.auth import access_level, can_manage_from_oauth, guild_ctx, oauth_guilds, require_session

router = APIRouter(prefix="/api")


async def audit(ctx, action: str, detail: str | dict | None = None) -> None:
    await ctx.bot.db.execute(
        "INSERT INTO dashboard_audit (guild_id, user_id, action, detail) VALUES (?, ?, ?, ?)",
        (ctx.gid, ctx.user_id, action, detail if isinstance(detail, str) or detail is None else jdump(detail)))


def icon_url(gid, icon) -> str | None:
    return f"https://cdn.discordapp.com/icons/{gid}/{icon}.png?size=128" if icon else None


def sid(x) -> str:
    """Snowflakes are sent as strings: JavaScript numbers lose precision above 2^53."""
    return str(x) if x is not None else None


# ---------------------------------------------------------------- identity
@router.get("/me")
async def me(request: Request):
    sess = await require_session(request)
    cfg = request.app.state.config
    u = sess.user
    avatar = f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png?size=64" if u.get("avatar") else None
    return {"id": u["id"], "username": u.get("username"), "name": u.get("global_name") or u.get("username"), "avatar": avatar,
            "csrf": sess.csrf_token, "is_owner": int(u["id"]) in cfg.owner_ids}


async def build_guild_list(request: Request, sess, refresh: bool = False) -> list[dict]:
    """Servers this user can open (or invite Nexora to). Shared by the API and the server-rendered page."""
    bot = request.app.state.bot
    out = []
    for g in await oauth_guilds(request, sess, force=refresh):
        gid = int(g["id"])
        live = bot.get_guild(gid) if bot else None
        level = await access_level(bot, live, sess.user_id) if live else None
        if level is None and not can_manage_from_oauth(g):
            continue
        access = level or ("invite" if not live else None)
        if not access:
            continue
        out.append({"id": g["id"], "name": g["name"], "icon": icon_url(g["id"], g["icon"]), "bot_in": live is not None,
                    "access": access, "members": live.member_count if live else None})
    out.sort(key=lambda g: (not g["bot_in"], g["name"].lower()))
    return out


@router.get("/guilds")
async def guilds(request: Request, refresh: bool = False):
    sess = await require_session(request)
    return {"guilds": await build_guild_list(request, sess, refresh), "invite_url": request.app.state.config.invite_url}


# ---------------------------------------------------------------- metadata
@router.get("/guilds/{gid}/meta")
async def meta(request: Request, gid: int):
    ctx = await guild_ctx(request, gid)
    g = ctx.guild
    me_member = g.me
    text_types = (discord.TextChannel, discord.ForumChannel)
    channels = [{"id": sid(c.id), "name": c.name, "category": c.category.name if c.category else None,
                 "type": "forum" if isinstance(c, discord.ForumChannel) else "text",
                 "can_send": c.permissions_for(me_member).send_messages if me_member else False}
                for c in sorted(g.channels, key=lambda c: (c.category.position if c.category else -1, c.position)) if isinstance(c, text_types)]
    voice = [{"id": sid(c.id), "name": c.name} for c in g.channels if isinstance(c, (discord.VoiceChannel, discord.StageChannel))]
    categories = [{"id": sid(c.id), "name": c.name} for c in g.categories]
    roles = [{"id": sid(r.id), "name": r.name, "color": f"#{r.color.value:06X}" if r.color.value else None,
              "managed": r.managed, "assignable": bool(me_member and r < me_member.top_role and not r.managed and not r.is_default())}
             for r in sorted(g.roles, key=lambda r: -r.position) if not r.is_default()]
    perms = me_member.guild_permissions if me_member else discord.Permissions.none()
    needed = ["manage_roles", "manage_channels", "kick_members", "ban_members", "moderate_members", "manage_messages",
              "view_audit_log", "manage_guild", "embed_links", "read_message_history"]
    return {
        "guild": {"id": sid(g.id), "name": g.name, "icon": g.icon.url if g.icon else None, "member_count": g.member_count,
                  "owner_id": sid(g.owner_id)},
        "access": ctx.level,
        "channels": channels, "voice_channels": voice, "categories": categories, "roles": roles,
        "modules": schema.MODULES,
        "automation": {"triggers": TRIGGERS, "conditions": CONDITIONS, "actions": ACTIONS},
        "missing_permissions": [p for p in needed if not getattr(perms, p) and not perms.administrator],
        "ai_available": bool(request.app.state.config.anthropic_api_key),
    }


# ---------------------------------------------------------------- settings
@router.get("/guilds/{gid}/settings/{module}")
async def get_settings(request: Request, gid: int, module: str):
    ctx = await guild_ctx(request, gid)
    if module not in schema.MODULES:
        raise HTTPException(404, "Unknown module")
    data = await ctx.bot.settings.get(gid, module)
    return {"module": module, "values": _stringify_ids(module, data)}


def _stringify_ids(module: str, data: dict) -> dict:
    out = dict(data)
    for f in schema.iter_fields(module):
        k, t = f["key"], f["type"]
        if t in {"channel", "role", "voice_channel", "category"}:
            out[k] = sid(out.get(k))
        elif t in {"channels", "roles"}:
            out[k] = [sid(v) for v in out.get(k) or []]
        elif t == "rows":
            rows = []
            for row in out.get(k) or []:
                r = dict(row)
                for col in f["columns"]:
                    if col["type"] in {"channel", "role"}:
                        r[col["key"]] = sid(r.get(col["key"]))
                rows.append(r)
            out[k] = rows
    return out


@router.put("/guilds/{gid}/settings/{module}")
async def put_settings(request: Request, gid: int, module: str):
    ctx = await guild_ctx(request, gid, write=True)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected an object")
    await _check_settings_permissions(ctx, module, body)
    try:
        data = await ctx.bot.settings.update(gid, module, body)
    except schema.ValidationError as exc:
        raise HTTPException(422, {"message": "Some settings are invalid", "errors": exc.errors}) from None
    await audit(ctx, f"settings.update:{module}", {"keys": sorted(body)})
    return {"ok": True, "values": _stringify_ids(module, data)}


async def _check_settings_permissions(ctx, module: str, values: dict) -> None:
    """Role rewards/autoroles and dashboard access can only be set by people allowed to grant them in Discord."""
    try:
        clean = schema.validate(module, values, partial=True)
    except schema.ValidationError:
        return  # the normal validation reports these
    problems = settings_problems(ctx.member, ctx.guild, module, clean, await ctx.bot.settings.get(ctx.gid, module))
    if problems:
        raise HTTPException(403, {"message": "You don't have permission to change some of these settings", "errors": problems})


@router.post("/guilds/{gid}/settings/{module}/reset")
async def reset_settings(request: Request, gid: int, module: str):
    ctx = await guild_ctx(request, gid, write=True)
    if module not in schema.MODULES:
        raise HTTPException(404, "Unknown module")
    data = await ctx.bot.settings.reset(gid, module)
    await audit(ctx, f"settings.reset:{module}")
    return {"ok": True, "values": _stringify_ids(module, data)}


# ---------------------------------------------------------------- overview
@router.get("/guilds/{gid}/overview")
async def overview(request: Request, gid: int):
    ctx = await guild_ctx(request, gid)
    db, g = ctx.bot.db, ctx.guild
    week = "datetime('now','-7 days')"
    q = lambda sql, *p: db.fetchval(sql, (gid, *p), 0)  # noqa: E731
    stats7 = await db.fetchone(f"SELECT COALESCE(SUM(joins),0) j, COALESCE(SUM(leaves),0) l, COALESCE(SUM(messages),0) m, "
                               f"COALESCE(SUM(voice_minutes),0) v FROM stats_daily WHERE guild_id=? AND day >= ?",
                               (gid, (date.today() - timedelta(days=6)).isoformat()))
    humans = sum(1 for m in g.members if not m.bot)
    recent = await db.fetchall("SELECT id, category, event_type, summary, created_at FROM log_events WHERE guild_id=? ORDER BY id DESC LIMIT 12", (gid,))
    staff = await db.fetchall(
        "SELECT user_id, SUM(actions) n FROM staff_activity WHERE guild_id=? AND day >= ? GROUP BY user_id ORDER BY n DESC LIMIT 8",
        (gid, (date.today() - timedelta(days=29)).isoformat()))
    bot = ctx.bot
    return {
        "members": {"total": g.member_count, "humans": humans, "bots": (g.member_count or 0) - humans,
                    "online": sum(1 for m in g.members if m.status != discord.Status.offline) if bot.intents.presences else None,
                    "boosts": g.premium_subscription_count},
        "week": {"joins": stats7["j"], "leaves": stats7["l"], "messages": stats7["m"], "voice_minutes": stats7["v"]},
        "bot": {"latency_ms": round(bot.latency * 1000) if bot.latency == bot.latency else None, "uptime": int(time.time() - bot.started_at),
                "ready": bot.is_ready()},
        "moderation": {"cases_7d": await q(f"SELECT COUNT(*) FROM mod_cases WHERE guild_id=? AND created_at > {week}"),
                       "active_warnings": await q("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND active=1"),
                       "pending_appeals": await q("SELECT COUNT(*) FROM appeals WHERE guild_id=? AND status='pending'"),
                       "automod_7d": await q(f"SELECT COUNT(*) FROM automod_flags WHERE guild_id=? AND created_at > {week}"),
                       "flags_pending": await q("SELECT COUNT(*) FROM automod_flags WHERE guild_id=? AND status='pending'")},
        "tickets": {"open": await q("SELECT COUNT(*) FROM tickets WHERE guild_id=? AND status='open'"),
                    "closed_7d": await q(f"SELECT COUNT(*) FROM tickets WHERE guild_id=? AND status='closed' AND closed_at > {week}"),
                    "avg_rating": await db.fetchval("SELECT ROUND(AVG(rating),2) FROM tickets WHERE guild_id=? AND rating IS NOT NULL", (gid,))},
        "applications": {"pending": await q("SELECT COUNT(*) FROM application_submissions WHERE guild_id=? AND status='pending'"),
                         "total_30d": await q("SELECT COUNT(*) FROM application_submissions WHERE guild_id=? AND created_at > datetime('now','-30 days')")},
        "giveaways": {"running": await q("SELECT COUNT(*) FROM giveaways WHERE guild_id=? AND status='running'"),
                      "entries_30d": await q("SELECT COUNT(*) FROM giveaway_entries e JOIN giveaways g ON g.id=e.giveaway_id WHERE g.guild_id=? AND e.created_at > datetime('now','-30 days')")},
        "recent_events": recent,
        "staff_activity": [{"user_id": sid(s["user_id"]), "name": _member_name(g, s["user_id"]), "actions": s["n"]} for s in staff],
    }


def _member_name(guild, uid) -> str:
    m = guild.get_member(int(uid)) if uid else None
    return m.display_name if m else f"User {uid}"


# --------------------------------------------------------------- analytics
def _range(days: int | None, start: str | None, end: str | None) -> tuple[date, date]:
    try:
        e = date.fromisoformat(end) if end else date.today()
        s = date.fromisoformat(start) if start else e - timedelta(days=(days or 30) - 1)
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD") from None
    if s > e or (e - s).days > 730:
        raise HTTPException(400, "Range must be at most 2 years and start before end")
    return s, e


async def analytics_data(ctx, s: date, e: date) -> dict:
    db, gid = ctx.bot.db, ctx.gid
    rows = {r["day"]: r for r in await db.fetchall("SELECT * FROM stats_daily WHERE guild_id=? AND day BETWEEN ? AND ?", (gid, s.isoformat(), e.isoformat()))}
    days = [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]
    series = {k: [rows.get(d, {}).get(k, 0) or 0 for d in days] for k in ("joins", "leaves", "messages", "voice_minutes", "mod_actions")}
    series["member_count"] = [rows.get(d, {}).get("member_count") for d in days]
    lo, hi = s.isoformat(), (e + timedelta(days=1)).isoformat()

    async def daily(sql: str) -> list[int]:
        got = {r["d"]: r["n"] for r in await db.fetchall(sql, (gid, lo, hi))}
        return [got.get(d, 0) for d in days]

    series["tickets_opened"] = await daily("SELECT substr(created_at,1,10) d, COUNT(*) n FROM tickets WHERE guild_id=? AND created_at >= ? AND created_at < ? GROUP BY d")
    series["applications"] = await daily("SELECT substr(created_at,1,10) d, COUNT(*) n FROM application_submissions WHERE guild_id=? AND created_at >= ? AND created_at < ? GROUP BY d")
    series["giveaway_entries"] = await daily("SELECT substr(e.created_at,1,10) d, COUNT(*) n FROM giveaway_entries e JOIN giveaways g ON g.id=e.giveaway_id WHERE g.guild_id=? AND e.created_at >= ? AND e.created_at < ? GROUP BY d")
    actions = await db.fetchall("SELECT action, COUNT(*) n FROM mod_cases WHERE guild_id=? AND created_at >= ? AND created_at < ? GROUP BY action ORDER BY n DESC", (gid, lo, hi))
    staff = await db.fetchall("SELECT user_id, SUM(actions) n FROM staff_activity WHERE guild_id=? AND day BETWEEN ? AND ? GROUP BY user_id ORDER BY n DESC LIMIT 15", (gid, s.isoformat(), e.isoformat()))
    features = await db.fetchall("SELECT feature, count FROM feature_usage WHERE guild_id=? ORDER BY count DESC LIMIT 20", (gid,))
    log_cats = await db.fetchall("SELECT category, COUNT(*) n FROM log_events WHERE guild_id=? AND created_at >= ? AND created_at < ? GROUP BY category ORDER BY n DESC", (gid, lo, hi))
    totals = {k: sum(v for v in vals if v) for k, vals in series.items() if k != "member_count"}
    totals["net_growth"] = totals["joins"] - totals["leaves"]
    return {"days": days, "series": series, "totals": totals, "mod_actions": actions, "log_categories": log_cats,
            "staff": [{"user_id": sid(r["user_id"]), "name": _member_name(ctx.guild, r["user_id"]), "actions": r["n"]} for r in staff],
            "features": features}


@router.get("/guilds/{gid}/analytics")
async def analytics(request: Request, gid: int, days: int | None = 30, start: str | None = None, end: str | None = None):
    ctx = await guild_ctx(request, gid)
    s, e = _range(days, start, end)
    return await analytics_data(ctx, s, e)


@router.get("/guilds/{gid}/analytics/export")
async def analytics_export(request: Request, gid: int, days: int | None = 30, start: str | None = None, end: str | None = None):
    ctx = await guild_ctx(request, gid)
    s, e = _range(days, start, end)
    data = await analytics_data(ctx, s, e)
    buf = io.StringIO()
    w = csv.writer(buf)
    keys = list(data["series"])
    w.writerow(["day", *keys])
    for i, d in enumerate(data["days"]):
        w.writerow([d, *[data["series"][k][i] if data["series"][k][i] is not None else "" for k in keys]])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="nexora-analytics-{gid}-{s}-{e}.csv"'})


# -------------------------------------------------------------- audit trail
@router.get("/guilds/{gid}/audit")
async def audit_log(request: Request, gid: int, limit: int = 100):
    ctx = await guild_ctx(request, gid)
    rows = await ctx.bot.db.fetchall("SELECT * FROM dashboard_audit WHERE guild_id=? ORDER BY id DESC LIMIT ?", (gid, max(1, min(limit, 500))))
    for r in rows:
        r["user_name"] = _member_name(ctx.guild, r["user_id"])
        r["user_id"] = sid(r["user_id"])
    return {"entries": rows}


# ----------------------------------------------------------- config backup
@router.get("/guilds/{gid}/config/export")
async def config_export(request: Request, gid: int):
    ctx = await guild_ctx(request, gid, write=True)
    db = ctx.bot.db
    payload = {
        "nexora_config_version": 1, "guild_id": str(gid), "exported_at": datetime.now(timezone.utc).isoformat(),
        "modules": await ctx.bot.settings.export_guild(gid),
        "ticket_panels": await db.fetchall("SELECT * FROM ticket_panels WHERE guild_id=?", (gid,)),
        "application_forms": await db.fetchall("SELECT * FROM application_forms WHERE guild_id=?", (gid,)),
        "role_menus": await db.fetchall("SELECT * FROM role_menus WHERE guild_id=?", (gid,)),
        "custom_commands": await db.fetchall("SELECT * FROM custom_commands WHERE guild_id=?", (gid,)),
        "automations": await db.fetchall("SELECT * FROM automations WHERE guild_id=?", (gid,)),
        "shop_items": await db.fetchall("SELECT * FROM shop_items WHERE guild_id=?", (gid,)),
        "saved_embeds": await db.fetchall("SELECT * FROM saved_embeds WHERE guild_id=?", (gid,)),
    }
    await audit(ctx, "config.export")
    return Response(jdump(payload), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="nexora-config-{gid}.json"'})


IMPORT_LIMITS = {"ticket_panels": 50, "application_forms": 200, "role_menus": 200, "custom_commands": 200,
                 "automations": 100, "shop_items": 200, "saved_embeds": 200}
IMPORT_JSON_COLS = {"staff_role_ids", "questions", "accept_role_ids", "remove_role_ids", "reviewer_role_ids", "options", "embed",
                    "required_role_ids", "conditions", "actions", "data"}


def _normalize_import_row(ctx, table: str, row: dict) -> dict:
    """Turn an exported database row back into the shape the dashboard sends, and drop references to
    channels/roles that don't exist in this server (backups can come from a different server)."""
    g = ctx.guild
    r = {k: (jload(v, None) if k in IMPORT_JSON_COLS and isinstance(v, str) else v) for k, v in row.items()}
    for k, v in list(r.items()):
        if k.endswith("channel_id") or k == "category_id":
            if v and not (str(v).isdigit() and g.get_channel(int(v))):
                r[k] = None
        elif k.endswith("role_ids") and isinstance(v, list):
            r[k] = [x for x in v if str(x).isdigit() and g.get_role(int(x))]
        elif k == "role_id" and v and not (str(v).isdigit() and g.get_role(int(v))):
            r[k] = None
    if table == "ticket_panels" and isinstance(r.get("color"), int):
        r["color"] = f"#{r['color']:06X}"
    if table == "role_menus" and isinstance(r.get("options"), list):
        r["options"] = [o for o in r["options"] if isinstance(o, dict) and str(o.get("role_id", "")).isdigit() and g.get_role(int(o["role_id"]))]
    return r


@router.post("/guilds/{gid}/config/import")
async def config_import(request: Request, gid: int):
    """Restore a backup. Every row goes through the same validation and permission checks as the dashboard pages,
    so a crafted backup file can't sneak in anything the editor couldn't set up by hand."""
    from nexora.web import api_features as feat

    ctx = await guild_ctx(request, gid, write=True)
    body = await request.json()
    if not isinstance(body, dict) or body.get("nexora_config_version") != 1:
        raise HTTPException(400, "That isn't a Nexora configuration backup")
    modules_in = body.get("modules") or {}
    if not isinstance(modules_in, dict):
        raise HTTPException(400, "That isn't a Nexora configuration backup")
    for module, values in modules_in.items():
        if module in schema.MODULES and isinstance(values, dict):
            await _check_settings_permissions(ctx, module, values)
    try:
        modules = await ctx.bot.settings.import_guild(gid, modules_in)
    except schema.ValidationError as exc:
        raise HTTPException(422, {"message": "Backup contains invalid settings", "errors": exc.errors}) from None
    bodies = {"ticket_panels": feat.ticket_panel_body, "application_forms": feat.form_body, "role_menus": feat.menu_body,
              "custom_commands": feat.command_body, "shop_items": feat.shop_body, "saved_embeds": feat.embed_save_body}
    counts, skipped = {}, []
    replace = bool(body.get("replace", False)) or request.query_params.get("replace") == "1"
    for table, limit in IMPORT_LIMITS.items():
        rows = body.get(table)
        if not isinstance(rows, list):
            continue
        if replace:
            await ctx.bot.db.execute(f"DELETE FROM {table} WHERE guild_id=?", (gid,))
        n = 0
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            if await ctx.bot.db.fetchval(f"SELECT COUNT(*) FROM {table} WHERE guild_id=?", (gid,), 0) >= limit:
                skipped.append(f"{table}: limit of {limit} reached")
                break
            norm = _normalize_import_row(ctx, table, row)
            try:
                if table == "automations":
                    await feat._save_automation(ctx, norm, None)
                else:
                    data = bodies[table](ctx, norm)
                    ph = ",".join("?" * (len(data) + 1))
                    await ctx.bot.db.execute(f"INSERT OR IGNORE INTO {table} (guild_id, {', '.join(data)}) VALUES ({ph})", (gid, *data.values()))
            except HTTPException as exc:
                label = norm.get("name") or norm.get("title") or f"row {n + 1}"
                skipped.append(f"{table} '{label}': {exc.detail}")
                continue
            n += 1
        counts[table] = n
    await audit(ctx, "config.import", {"modules": modules, "rows": counts, "skipped": len(skipped)})
    auto = ctx.bot.get_cog("Automation")
    if auto:
        auto.invalidate(gid)
    return {"ok": True, "modules": modules, "rows": counts, "skipped": skipped[:50],
            "note": "Panels, forms and role menus were imported; publish them again from their pages to post the messages."}


# ------------------------------------------------------------------ system
async def require_owner(request: Request):
    sess = await require_session(request)
    if sess.user_id not in request.app.state.config.owner_ids:
        raise HTTPException(403, "Only the bot owner (BOT_OWNER_IDS) can use system tools")
    return sess


@router.get("/system")
async def system(request: Request):
    await require_owner(request)
    st = request.app.state
    bot, db, cfg = st.bot, st.db, st.config
    t = time.perf_counter()
    await db.fetchval("SELECT 1")
    db_ms = (time.perf_counter() - t) * 1000
    size = os.path.getsize(cfg.database_path) if os.path.exists(cfg.database_path) else 0
    errors = await db.fetchall("SELECT id, source, guild_id, message, created_at FROM error_log ORDER BY id DESC LIMIT 50")
    for e in errors:
        e["guild_id"] = sid(e["guild_id"])
    features = await db.fetchall("SELECT feature, SUM(count) n FROM feature_usage GROUP BY feature ORDER BY n DESC LIMIT 30")
    tables = {}
    for tname in ("log_events", "mod_cases", "tickets", "application_submissions", "giveaways", "levels", "economy", "web_sessions"):
        tables[tname] = await db.fetchval(f"SELECT COUNT(*) FROM {tname}", (), 0)
    return {
        "bot": {"ready": bool(bot and bot.is_ready()), "user": str(bot.user) if bot and bot.user else None,
                "guilds": len(bot.guilds) if bot else 0, "latency_ms": round(bot.latency * 1000) if bot and bot.latency == bot.latency else None,
                "uptime": int(time.time() - bot.started_at) if bot else 0, "commands_run": bot.command_count if bot else 0,
                "cogs": sorted(bot.cogs) if bot else []},
        "database": {"path": str(cfg.database_path), "size_bytes": size, "query_ms": round(db_ms, 2), "rows": tables},
        "errors": errors, "feature_usage": features,
        "backups": _list_backups(cfg.backup_dir),
    }


def _list_backups(folder: Path) -> list[dict]:
    return [{"name": p.name, "size_bytes": p.stat().st_size, "created": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
            for p in sorted(folder.glob("nexora-*.db"), reverse=True)]


@router.post("/system/backup")
async def system_backup(request: Request):
    await require_owner(request)
    st = request.app.state
    name = f"nexora-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
    await st.db.backup(st.config.backup_dir / name)
    return {"ok": True, "name": name, "backups": _list_backups(st.config.backup_dir)}


@router.post("/system/restore")
async def system_restore(request: Request):
    await require_owner(request)
    st = request.app.state
    name = (await request.json()).get("name", "")
    path = (st.config.backup_dir / name).resolve()
    if path.parent != st.config.backup_dir.resolve() or not path.name.startswith("nexora-") or not path.exists():
        raise HTTPException(404, "Backup not found")
    safety = f"nexora-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-pre-restore.db"
    await st.db.backup(st.config.backup_dir / safety)
    try:
        await st.db.restore(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    st.bot.settings.invalidate()
    return {"ok": True, "restored": name, "safety_backup": safety}


@router.post("/system/errors/clear")
async def clear_errors(request: Request):
    await require_owner(request)
    await request.app.state.db.execute("DELETE FROM error_log")
    return {"ok": True}
