"""Dashboard API: logs, moderation cases/warnings/notes/appeals, automod review, moderation actions."""
from __future__ import annotations

import csv
import io
from datetime import timedelta

import discord
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from nexora.core.utils import parse_duration
from nexora.db import jdump, jload
from nexora.web.api_core import _member_name, audit
from nexora.web.auth import guild_ctx

router = APIRouter(prefix="/api/guilds/{gid}")


def _user_label(guild, uid):
    if not uid:
        return None
    m = guild.get_member(int(uid))
    return {"id": str(uid), "name": m.display_name if m else None, "tag": str(m) if m else None,
            "avatar": m.display_avatar.url if m else None}


# -------------------------------------------------------------------- logs
def _log_filters(gid: int, category, event_type, q, user_id, moderator_id, channel_id, since, until):
    where, params = ["guild_id = ?"], [gid]
    if category:
        cats = [c for c in category.split(",") if c]
        where.append(f"category IN ({','.join('?' * len(cats))})")
        params += cats
    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    if q:
        where.append("(summary LIKE ? OR data LIKE ?)")
        params += [f"%{q[:100]}%", f"%{q[:100]}%"]
    for col, val in (("user_id", user_id), ("moderator_id", moderator_id), ("channel_id", channel_id)):
        if val:
            if not str(val).isdigit():
                raise HTTPException(400, f"{col} must be numeric")
            where.append(f"{col} = ?")
            params.append(int(val))
    if since:
        where.append("created_at >= ?")
        params.append(since.replace("T", " ")[:19])
    if until:
        where.append("created_at <= ?")
        params.append(until.replace("T", " ")[:19] if len(until) > 10 else until + " 23:59:59")
    return " AND ".join(where), params


@router.get("/logs")
async def logs(request: Request, gid: int, category: str | None = None, event_type: str | None = None, q: str | None = None,
               user_id: str | None = None, moderator_id: str | None = None, channel_id: str | None = None,
               since: str | None = None, until: str | None = None, before_id: int | None = None, limit: int = 50):
    ctx = await guild_ctx(request, gid)
    where, params = _log_filters(gid, category, event_type, q, user_id, moderator_id, channel_id, since, until)
    if before_id:
        where += " AND id < ?"
        params.append(before_id)
    limit = max(1, min(limit, 200))
    rows = await ctx.bot.db.fetchall(f"SELECT * FROM log_events WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit))
    total = await ctx.bot.db.fetchval(f"SELECT COUNT(*) FROM log_events WHERE {where.replace(' AND id < ?', '')}",
                                      params[:-1] if before_id else params, 0)
    for r in rows:
        r["data"] = jload(r["data"], {})
        r["user"] = _user_label(ctx.guild, r["user_id"])
        r["moderator"] = _user_label(ctx.guild, r["moderator_id"])
        ch = ctx.guild.get_channel(r["channel_id"]) if r["channel_id"] else None
        r["channel_name"] = ch.name if ch else None
    types = await ctx.bot.db.fetchall("SELECT DISTINCT event_type FROM log_events WHERE guild_id=? ORDER BY event_type", (gid,))
    return {"events": rows, "total": total, "event_types": [t["event_type"] for t in types], "next_before_id": rows[-1]["id"] if len(rows) == limit else None}


@router.get("/logs/export")
async def logs_export(request: Request, gid: int, format: str = "csv", category: str | None = None, event_type: str | None = None,
                      q: str | None = None, user_id: str | None = None, moderator_id: str | None = None, channel_id: str | None = None,
                      since: str | None = None, until: str | None = None):
    ctx = await guild_ctx(request, gid)
    where, params = _log_filters(gid, category, event_type, q, user_id, moderator_id, channel_id, since, until)
    rows = await ctx.bot.db.fetchall(f"SELECT * FROM log_events WHERE {where} ORDER BY id DESC LIMIT 50000", params)
    await audit(ctx, "logs.export", {"format": format, "rows": len(rows)})
    if format == "json":
        for r in rows:
            r["data"] = jload(r["data"], {})
            for k in ("guild_id", "user_id", "moderator_id", "channel_id"):
                r[k] = str(r[k]) if r[k] else None
        return Response(jdump(rows), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="nexora-logs-{gid}.json"'})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at_utc", "category", "event_type", "user_id", "moderator_id", "channel_id", "summary"])
    for r in rows:
        w.writerow([r["id"], r["created_at"], r["category"], r["event_type"], r["user_id"] or "", r["moderator_id"] or "", r["channel_id"] or "", r["summary"]])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="nexora-logs-{gid}.csv"'})


# ------------------------------------------------------------------- cases
@router.get("/cases")
async def cases(request: Request, gid: int, user_id: str | None = None, action: str | None = None, moderator_id: str | None = None, limit: int = 100):
    ctx = await guild_ctx(request, gid)
    where, params = ["guild_id=?"], [gid]
    if user_id and user_id.isdigit():
        where.append("user_id=?")
        params.append(int(user_id))
    if moderator_id and moderator_id.isdigit():
        where.append("moderator_id=?")
        params.append(int(moderator_id))
    if action:
        where.append("action=?")
        params.append(action)
    rows = await ctx.bot.db.fetchall(f"SELECT * FROM mod_cases WHERE {' AND '.join(where)} ORDER BY case_number DESC LIMIT ?", (*params, max(1, min(limit, 500))))
    for r in rows:
        r["user"] = _user_label(ctx.guild, r["user_id"])
        r["moderator"] = _user_label(ctx.guild, r["moderator_id"])
    stats = await ctx.bot.db.fetchall(
        "SELECT moderator_id, COUNT(*) n FROM mod_cases WHERE guild_id=? AND moderator_id IS NOT NULL AND created_at > datetime('now','-30 days') GROUP BY moderator_id ORDER BY n DESC LIMIT 10", (gid,))
    return {"cases": rows, "moderators": [{"user_id": str(s["moderator_id"]), "name": _member_name(ctx.guild, s["moderator_id"]), "actions": s["n"]} for s in stats]}


@router.patch("/cases/{number}")
async def edit_case(request: Request, gid: int, number: int):
    ctx = await guild_ctx(request, gid, write=True)
    body = await request.json()
    reason = str(body.get("reason", ""))[:1000]
    n = await ctx.bot.db.execute_rowcount("UPDATE mod_cases SET reason=? WHERE guild_id=? AND case_number=?", (reason, gid, number))
    if not n:
        raise HTTPException(404, "Case not found")
    await ctx.bot.db.execute("UPDATE warnings SET reason=? WHERE guild_id=? AND case_number=?", (reason, gid, number))
    await audit(ctx, "case.edit", {"case": number})
    return {"ok": True}


@router.get("/warnings")
async def warnings(request: Request, gid: int, user_id: str | None = None, include_inactive: bool = False):
    ctx = await guild_ctx(request, gid)
    where, params = "guild_id=?", [gid]
    if user_id and user_id.isdigit():
        where += " AND user_id=?"
        params.append(int(user_id))
    if not include_inactive:
        where += " AND active=1"
    rows = await ctx.bot.db.fetchall(f"SELECT * FROM warnings WHERE {where} ORDER BY id DESC LIMIT 300", params)
    for r in rows:
        r["user"] = _user_label(ctx.guild, r["user_id"])
        r["moderator"] = _user_label(ctx.guild, r["moderator_id"])
    return {"warnings": rows}


@router.delete("/warnings/{wid}")
async def delete_warning(request: Request, gid: int, wid: int):
    ctx = await guild_ctx(request, gid, write=True)
    n = await ctx.bot.db.execute_rowcount("UPDATE warnings SET active=0 WHERE guild_id=? AND id=?", (gid, wid))
    if not n:
        raise HTTPException(404, "Warning not found")
    await audit(ctx, "warning.remove", {"id": wid})
    return {"ok": True}


@router.get("/users/{user_id}")
async def user_profile(request: Request, gid: int, user_id: int):
    ctx = await guild_ctx(request, gid)
    db = ctx.bot.db
    m = ctx.guild.get_member(user_id)
    out = {"user": _user_label(ctx.guild, user_id), "in_server": m is not None,
           "joined_at": m.joined_at.isoformat() if m and m.joined_at else None,
           "created_at": discord.utils.snowflake_time(user_id).isoformat(),
           "roles": [{"id": str(r.id), "name": r.name} for r in m.roles if not r.is_default()] if m else [],
           "cases": await db.fetchall("SELECT * FROM mod_cases WHERE guild_id=? AND user_id=? ORDER BY case_number DESC LIMIT 50", (gid, user_id)),
           "warnings": await db.fetchall("SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 50", (gid, user_id)),
           "notes": await db.fetchall("SELECT * FROM mod_notes WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 50", (gid, user_id)),
           "level": await db.fetchone("SELECT xp, level, messages FROM levels WHERE guild_id=? AND user_id=?", (gid, user_id)),
           "balance": await db.fetchval("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (gid, user_id)),
           "recent_logs": await db.fetchall("SELECT id, category, event_type, summary, created_at FROM log_events WHERE guild_id=? AND (user_id=? OR moderator_id=?) ORDER BY id DESC LIMIT 25", (gid, user_id, user_id))}
    for n in out["notes"]:
        n["moderator"] = _user_label(ctx.guild, n["moderator_id"])
    return out


@router.get("/members/search")
async def member_search(request: Request, gid: int, q: str = ""):
    ctx = await guild_ctx(request, gid)
    q = q.lower().strip()
    if q.isdigit():
        m = ctx.guild.get_member(int(q))
        return {"members": [_user_label(ctx.guild, m.id)] if m else [{"id": q, "name": None, "tag": None, "avatar": None}]}
    found = [m for m in ctx.guild.members if q and (q in m.name.lower() or q in m.display_name.lower())][:15]
    return {"members": [_user_label(ctx.guild, m.id) for m in found]}


@router.post("/notes")
async def add_note(request: Request, gid: int):
    ctx = await guild_ctx(request, gid, write=True)
    body = await request.json()
    uid, note = str(body.get("user_id", "")), str(body.get("note", "")).strip()[:1500]
    if not uid.isdigit() or not note:
        raise HTTPException(400, "user_id and note are required")
    await ctx.bot.db.execute("INSERT INTO mod_notes (guild_id, user_id, moderator_id, note) VALUES (?, ?, ?, ?)", (gid, int(uid), ctx.user_id, note))
    await audit(ctx, "note.add", {"user_id": uid})
    return {"ok": True}


@router.post("/moderation/action")
async def mod_action(request: Request, gid: int):
    """Run a moderation action from the dashboard. Discord permission & hierarchy checks are applied to the dashboard user."""
    ctx = await guild_ctx(request, gid, write=True)
    body = await request.json()
    action, uid = body.get("action"), str(body.get("user_id", ""))
    reason = (str(body.get("reason") or "").strip()[:500]) or None
    if not uid.isdigit():
        raise HTTPException(400, "Invalid user ID")
    g = ctx.guild
    actor = g.get_member(ctx.user_id)
    mod = ctx.bot.get_cog("Moderation")
    needed = {"warn": "moderate_members", "timeout": "moderate_members", "untimeout": "moderate_members",
              "kick": "kick_members", "ban": "ban_members", "unban": "ban_members"}
    if action not in needed:
        raise HTTPException(400, "Unknown action")
    if not actor or not getattr(actor.guild_permissions, needed[action]):
        raise HTTPException(403, f"You need the {needed[action].replace('_', ' ').title()} permission in Discord")
    s = await ctx.bot.settings.get(gid, "moderation")
    if s["require_reason"] and not reason:
        raise HTTPException(400, "A reason is required on this server")
    target = g.get_member(int(uid))
    if target and action != "unban":
        from nexora.bot.helpers import can_act_on
        problem = can_act_on(actor, target)
        if problem:
            raise HTTPException(403, problem)
    elif action not in {"ban", "unban"}:
        raise HTTPException(404, "That user isn't in the server")
    d = parse_duration(body.get("duration")) if body.get("duration") else None
    audit_reason = f"{actor} via dashboard: {reason or 'No reason'}"
    try:
        if action == "warn":
            case, count, esc = await mod.issue_warning(g, target, actor, reason)
            msg = f"Warned (case #{case}, {count} active)" + (f"; escalation: {esc}" if esc else "")
        elif action == "timeout":
            d = min(d or timedelta(minutes=s["default_timeout_minutes"]), timedelta(days=28))
            await target.timeout(d, reason=audit_reason)
            case = await mod.create_case(g, "timeout", target, actor, reason, d)
            await mod.dm_user(g, target, "timeout", reason, actor, d, case)
            msg = f"Timed out (case #{case})"
        elif action == "untimeout":
            await target.timeout(None, reason=audit_reason)
            msg = f"Timeout removed (case #{await mod.create_case(g, 'untimeout', target, actor, reason)})"
        elif action == "kick":
            await mod.dm_user(g, target, "kick", reason, actor)
            await target.kick(reason=audit_reason)
            msg = f"Kicked (case #{await mod.create_case(g, 'kick', target, actor, reason)})"
        elif action == "ban":
            user = target or discord.Object(int(uid))
            if target:
                await mod.dm_user(g, target, "tempban" if d else "ban", reason, actor, d)
            await g.ban(user, reason=audit_reason, delete_message_seconds=0)
            msg = f"Banned (case #{await mod.create_case(g, 'tempban' if d else 'ban', int(uid), actor, reason, d)})"
        else:
            await g.unban(discord.Object(int(uid)), reason=audit_reason)
            await ctx.bot.db.execute("UPDATE mod_cases SET active=0 WHERE guild_id=? AND user_id=? AND action IN ('ban','tempban')", (gid, int(uid)))
            msg = f"Unbanned (case #{await mod.create_case(g, 'unban', int(uid), actor, reason)})"
    except discord.NotFound:
        raise HTTPException(404, "Discord couldn't find that user/ban") from None
    except discord.Forbidden:
        raise HTTPException(403, "Discord refused: check Nexora's role position and permissions") from None
    await audit(ctx, f"moderation.{action}", {"user_id": uid, "reason": reason})
    return {"ok": True, "message": msg}


# ----------------------------------------------------------------- appeals
@router.get("/appeals")
async def appeals(request: Request, gid: int, status: str = "pending"):
    ctx = await guild_ctx(request, gid)
    rows = await ctx.bot.db.fetchall(
        "SELECT a.*, c.action AS case_action, c.reason AS case_reason FROM appeals a LEFT JOIN mod_cases c ON c.guild_id=a.guild_id AND c.case_number=a.case_number "
        "WHERE a.guild_id=? AND (?='all' OR a.status=?) ORDER BY a.id DESC LIMIT 100", (gid, status, status))
    for r in rows:
        r["user"] = _user_label(ctx.guild, r["user_id"])
        user = ctx.bot.get_user(r["user_id"])
        if user and not r["user"]["name"]:
            r["user"]["tag"] = str(user)
    return {"appeals": rows}


@router.post("/appeals/{aid}/resolve")
async def resolve_appeal(request: Request, gid: int, aid: int):
    ctx = await guild_ctx(request, gid, write=True)
    body = await request.json()
    actor = ctx.guild.get_member(ctx.user_id)
    if not actor or not actor.guild_permissions.ban_members:
        raise HTTPException(403, "You need Ban Members to resolve appeals")
    result = await ctx.bot.get_cog("Moderation").resolve_appeal(ctx.guild, aid, bool(body.get("accept")), actor, str(body.get("response") or "")[:1000] or None)
    await audit(ctx, "appeal.resolve", {"id": aid, "accept": bool(body.get("accept"))})
    return {"ok": True, "message": result}


# ------------------------------------------------------------------ automod
@router.get("/automod/flags")
async def flags(request: Request, gid: int, status: str = "all"):
    ctx = await guild_ctx(request, gid)
    rows = await ctx.bot.db.fetchall("SELECT * FROM automod_flags WHERE guild_id=? AND (?='all' OR status=?) ORDER BY id DESC LIMIT 200", (gid, status, status))
    for r in rows:
        r["user"] = _user_label(ctx.guild, r["user_id"])
    return {"flags": rows}


@router.post("/automod/flags/{fid}/review")
async def review_flag(request: Request, gid: int, fid: int):
    ctx = await guild_ctx(request, gid, write=True)
    body = await request.json()
    actor = ctx.guild.get_member(ctx.user_id)
    result = await ctx.bot.get_cog("Automod").review_flag(ctx.guild, fid, bool(body.get("correct")), actor)
    await audit(ctx, "automod.review", {"id": fid, "correct": bool(body.get("correct"))})
    return {"ok": True, "message": result}
