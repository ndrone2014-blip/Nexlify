"""Dashboard API for feature systems. Every write validates input and is recorded in the audit trail."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import discord
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from nexora.core.automation_engine import validate_automation
from nexora.core.utils import color_int, embed_from_dict, parse_duration
from nexora.db import from_db_time, jdump, jload, to_db_time, utcnow
from nexora.web.api_core import audit
from nexora.web.api_moderation import _user_label
from nexora.web.auth import guild_ctx

router = APIRouter(prefix="/api/guilds/{gid}")


# ------------------------------------------------------------------ helpers
def s_(v, field: str, max_len: int, required: bool = False) -> str:
    v = "" if v is None else str(v).strip()
    if required and not v:
        raise HTTPException(422, f"{field} is required")
    if len(v) > max_len:
        raise HTTPException(422, f"{field} must be at most {max_len} characters")
    return v


def i_(v, field: str, lo: int, hi: int, default: int | None = None) -> int:
    if v in (None, "") and default is not None:
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be a number") from None
    if not lo <= n <= hi:
        raise HTTPException(422, f"{field} must be between {lo} and {hi}")
    return n


def snow(v, field: str, required: bool = False) -> int | None:
    if v in (None, "", 0, "0"):
        if required:
            raise HTTPException(422, f"{field} is required")
        return None
    if not str(v).isdigit():
        raise HTTPException(422, f"{field} must be a Discord ID")
    return int(v)


def snow_list(v, field: str) -> list[int]:
    if not v:
        return []
    if not isinstance(v, list):
        raise HTTPException(422, f"{field} must be a list")
    return list(dict.fromkeys(snow(x, field) for x in v if x))[:50]


def check_channel(ctx, cid: int | None, field: str = "channel", kinds=(discord.TextChannel,)) -> int | None:
    if cid is None:
        return None
    if not isinstance(ctx.guild.get_channel(cid), kinds):
        raise HTTPException(422, f"{field}: channel not found in this server")
    return cid


def check_roles(ctx, ids: list[int], field: str) -> list[int]:
    for r in ids:
        if not ctx.guild.get_role(r):
            raise HTTPException(422, f"{field}: role {r} not found")
    return ids


def decode(row: dict | None, *cols: str) -> dict | None:
    if row:
        for c in cols:
            row[c] = jload(row[c], [] if not c.endswith("roles") else {})
    return row


def validate_questions(qs, max_q: int, allow_choice: bool) -> list[dict]:
    if not isinstance(qs, list):
        raise HTTPException(422, "questions must be a list")
    if len(qs) > max_q:
        raise HTTPException(422, f"At most {max_q} questions")
    out = []
    for n, q in enumerate(qs, 1):
        t = q.get("type", "short")
        if t not in ({"short", "long", "choice"} if allow_choice else {"short", "long"}):
            raise HTTPException(422, f"Question {n}: invalid type")
        item = {"label": s_(q.get("label"), f"Question {n} label", 45, True), "type": t,
                "placeholder": s_(q.get("placeholder"), f"Question {n} placeholder", 100), "required": bool(q.get("required", True))}
        if t == "choice":
            opts = [s_(o, f"Question {n} option", 100) for o in (q.get("options") or []) if str(o).strip()]
            if not 2 <= len(opts) <= 25:
                raise HTTPException(422, f"Question {n}: multiple choice needs 2-25 options")
            item["options"] = opts
        else:
            item["long"] = t == "long"
            item["min_length"] = i_(q.get("min_length"), f"Question {n} min length", 0, 4000, 0)
            item["max_length"] = i_(q.get("max_length"), f"Question {n} max length", 1, 4000, 2000 if t == "long" else 300)
        out.append(item)
    return out


def validate_embed(data) -> dict:
    """Validate embed-builder JSON and enforce Discord's limits."""
    if not isinstance(data, dict):
        raise HTTPException(422, "embed must be an object")
    url_ok = lambda u: not u or str(u).startswith(("http://", "https://"))  # noqa: E731
    out = {
        "content": s_(data.get("content"), "Message content", 2000),
        "title": s_(data.get("title"), "Title", 256), "description": s_(data.get("description"), "Description", 4096),
        "url": s_(data.get("url"), "Title URL", 1000), "color": s_(data.get("color") or "#7C3AED", "Color", 7),
        "image": s_(data.get("image"), "Image URL", 1000), "thumbnail": s_(data.get("thumbnail"), "Thumbnail URL", 1000),
        "timestamp": bool(data.get("timestamp")),
        "author": {"name": s_((data.get("author") or {}).get("name"), "Author name", 256), "url": s_((data.get("author") or {}).get("url"), "Author URL", 1000),
                   "icon_url": s_((data.get("author") or {}).get("icon_url"), "Author icon", 1000)},
        "footer": {"text": s_((data.get("footer") or {}).get("text"), "Footer", 2048), "icon_url": s_((data.get("footer") or {}).get("icon_url"), "Footer icon", 1000)},
        "fields": [], "buttons": [],
    }
    for u in (out["url"], out["image"], out["thumbnail"], out["author"]["url"], out["author"]["icon_url"], out["footer"]["icon_url"]):
        if not url_ok(u):
            raise HTTPException(422, "URLs must start with http:// or https://")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", out["color"]):
        raise HTTPException(422, "Color must be a hex value like #7C3AED")
    for f in (data.get("fields") or [])[:25]:
        out["fields"].append({"name": s_(f.get("name"), "Field name", 256, True), "value": s_(f.get("value"), "Field value", 1024, True), "inline": bool(f.get("inline"))})
    for b in (data.get("buttons") or [])[:5]:
        url = s_(b.get("url"), "Button URL", 512, True)
        if not url.startswith(("http://", "https://")):
            raise HTTPException(422, "Button URLs must start with http:// or https://")
        out["buttons"].append({"label": s_(b.get("label"), "Button label", 80, True), "url": url})
    total = len(out["title"]) + len(out["description"]) + len(out["footer"]["text"]) + len(out["author"]["name"]) + sum(len(f["name"]) + len(f["value"]) for f in out["fields"])
    if total > 6000:
        raise HTTPException(422, "Embeds can contain at most 6000 characters in total")
    if not any([out["content"], out["title"], out["description"], out["fields"], out["image"], out["author"]["name"]]):
        raise HTTPException(422, "The message is empty")
    return out


def need_cog(ctx, name: str):
    cog = ctx.bot.get_cog(name)
    if cog is None or not ctx.bot.is_ready():
        raise HTTPException(409, "The bot isn't connected right now; try again shortly.")
    return cog


# ================================================================== tickets
def ticket_panel_body(ctx, b: dict) -> dict:
    color = s_(b.get("color"), "Color", 7)
    return {
        "name": s_(b.get("name"), "Name", 80, True),
        "channel_id": check_channel(ctx, snow(b.get("channel_id"), "Channel"), "Channel"),
        "category_id": check_channel(ctx, snow(b.get("category_id"), "Category"), "Category", (discord.CategoryChannel,)),
        "staff_role_ids": jdump(check_roles(ctx, snow_list(b.get("staff_role_ids"), "Staff roles"), "Staff roles")),
        "title": s_(b.get("title") or "Support", "Title", 256), "description": s_(b.get("description"), "Description", 4000),
        "button_label": s_(b.get("button_label") or "Open Ticket", "Button label", 80), "button_emoji": s_(b.get("button_emoji"), "Emoji", 64) or None,
        "color": color_int(color) if color else None,
        "questions": jdump(validate_questions(b.get("questions") or [], 5, False)),
        "max_open_per_user": i_(b.get("max_open_per_user"), "Max open tickets per user", 0, 20, 1),
        "welcome_message": s_(b.get("welcome_message") or "Hello {user}! Staff will be with you shortly.", "Welcome message", 2000),
        "name_format": s_(b.get("name_format") or "ticket-{number}", "Channel name format", 90),
        "enabled": int(bool(b.get("enabled", True))),
    }


def _panel_out(r: dict) -> dict:
    decode(r, "staff_role_ids", "questions")
    r["color"] = f"#{r['color']:06X}" if r["color"] is not None else ""
    return r


@router.get("/ticket-panels")
async def list_panels(request: Request, gid: int):
    ctx = await guild_ctx(request, gid)
    return {"panels": [_panel_out(r) for r in await ctx.bot.db.fetchall("SELECT * FROM ticket_panels WHERE guild_id=? ORDER BY id", (gid,))]}


@router.post("/ticket-panels")
async def create_panel(request: Request, gid: int):
    ctx = await guild_ctx(request, gid, write=True)
    if await ctx.bot.db.fetchval("SELECT COUNT(*) FROM ticket_panels WHERE guild_id=?", (gid,), 0) >= 50:
        raise HTTPException(422, "Limit of 50 panels reached")
    data = ticket_panel_body(ctx, await request.json())
    cols = ", ".join(data)
    pid = await ctx.bot.db.execute(f"INSERT INTO ticket_panels (guild_id, {cols}) VALUES (?, {','.join('?' * len(data))})", (gid, *data.values()))
    await audit(ctx, "tickets.panel.create", {"id": pid, "name": data["name"]})
    return {"ok": True, "id": pid}


@router.put("/ticket-panels/{pid}")
async def update_panel(request: Request, gid: int, pid: int):
    ctx = await guild_ctx(request, gid, write=True)
    data = ticket_panel_body(ctx, await request.json())
    n = await ctx.bot.db.execute_rowcount(f"UPDATE ticket_panels SET {', '.join(f'{k}=?' for k in data)} WHERE id=? AND guild_id=?", (*data.values(), pid, gid))
    if not n:
        raise HTTPException(404, "Panel not found")
    await audit(ctx, "tickets.panel.update", {"id": pid})
    return {"ok": True}


@router.delete("/ticket-panels/{pid}")
async def delete_panel(request: Request, gid: int, pid: int):
    ctx = await guild_ctx(request, gid, write=True)
    panel = await ctx.bot.db.fetchone("SELECT * FROM ticket_panels WHERE id=? AND guild_id=?", (pid, gid))
    if not panel:
        raise HTTPException(404, "Panel not found")
    await ctx.bot.db.execute("DELETE FROM ticket_panels WHERE id=?", (pid,))
    ch = ctx.guild.get_channel(panel["channel_id"] or 0)
    if isinstance(ch, discord.TextChannel) and panel["message_id"]:
        try:
            await ch.get_partial_message(panel["message_id"]).delete()
        except discord.HTTPException:
            pass
    await audit(ctx, "tickets.panel.delete", {"id": pid})
    return {"ok": True}


@router.post("/ticket-panels/{pid}/publish")
async def publish_panel(request: Request, gid: int, pid: int):
    ctx = await guild_ctx(request, gid, write=True)
    if not await ctx.bot.db.fetchval("SELECT 1 FROM ticket_panels WHERE id=? AND guild_id=?", (pid, gid)):
        raise HTTPException(404, "Panel not found")
    try:
        msg = await need_cog(ctx, "Tickets").publish_panel(pid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except discord.Forbidden:
        raise HTTPException(403, "Nexora can't send messages in that channel") from None
    await audit(ctx, "tickets.panel.publish", {"id": pid})
    return {"ok": True, "url": msg.jump_url}


@router.get("/tickets")
async def list_tickets(request: Request, gid: int, status: str = "all", limit: int = 100):
    ctx = await guild_ctx(request, gid)
    rows = await ctx.bot.db.fetchall(
        "SELECT t.id, t.number, t.status, t.priority, t.opener_id, t.claimed_by, t.rating, t.created_at, t.closed_at, t.close_reason, t.channel_id, "
        "t.transcript IS NOT NULL AS has_transcript, p.name AS panel FROM tickets t LEFT JOIN ticket_panels p ON p.id=t.panel_id "
        "WHERE t.guild_id=? AND (?='all' OR t.status=?) ORDER BY t.id DESC LIMIT ?", (gid, status, status, max(1, min(limit, 500))))
    for r in rows:
        r["opener"] = _user_label(ctx.guild, r["opener_id"])
        r["claimer"] = _user_label(ctx.guild, r["claimed_by"])
    stats = await ctx.bot.db.fetchone(
        "SELECT COUNT(*) total, SUM(status='open') open, ROUND(AVG(rating),2) avg_rating, "
        "ROUND(AVG((julianday(closed_at)-julianday(created_at))*24),1) avg_hours FROM tickets WHERE guild_id=?", (gid,))
    return {"tickets": rows, "stats": stats}


@router.get("/tickets/{tid}")
async def ticket_detail(request: Request, gid: int, tid: int):
    ctx = await guild_ctx(request, gid)
    t = await ctx.bot.db.fetchone("SELECT * FROM tickets WHERE id=? AND guild_id=?", (tid, gid))
    if not t:
        raise HTTPException(404, "Ticket not found")
    t["answers"] = jload(t["answers"], [])
    t["has_transcript"] = bool(t.pop("transcript"))
    t["opener"] = _user_label(ctx.guild, t["opener_id"])
    t["claimer"] = _user_label(ctx.guild, t["claimed_by"])
    return t


@router.post("/tickets/{tid}/close")
async def close_ticket(request: Request, gid: int, tid: int):
    ctx = await guild_ctx(request, gid, write=True)
    t = await ctx.bot.db.fetchone("SELECT * FROM tickets WHERE id=? AND guild_id=?", (tid, gid))
    if not t:
        raise HTTPException(404, "Ticket not found")
    reason = s_((await request.json()).get("reason"), "Reason", 500) or "Closed from dashboard"
    actor = ctx.guild.get_member(ctx.user_id)
    cog = need_cog(ctx, "Tickets")
    import asyncio
    asyncio.create_task(cog.close(t, actor, reason))  # channel deletion delay shouldn't block the request
    await audit(ctx, "tickets.close", {"id": tid})
    return {"ok": True}


transcript_router = APIRouter()


@transcript_router.get("/dashboard/{gid}/tickets/{tid}/transcript", response_class=HTMLResponse)
async def transcript(request: Request, gid: int, tid: int):
    ctx = await guild_ctx(request, gid)
    html = await ctx.bot.db.fetchval("SELECT transcript FROM tickets WHERE id=? AND guild_id=?", (tid, gid))
    if not html:
        raise HTTPException(404, "No transcript (tickets get one when they close)")
    # Transcript HTML is generated server-side with every user value escaped; serve it sandboxed anyway.
    return HTMLResponse(html, headers={"Content-Security-Policy": "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; sandbox"})


# ============================================================= applications
def form_body(ctx, b: dict) -> dict:
    kind = b.get("kind", "custom")
    from nexora.bot.cogs.applications import FORM_KINDS
    if kind not in FORM_KINDS:
        raise HTTPException(422, "Invalid form type")
    return {
        "name": s_(b.get("name"), "Name", 80, True), "kind": kind, "description": s_(b.get("description"), "Description", 2000),
        "questions": jdump(validate_questions(b.get("questions") or [], 100, True)),
        "review_channel_id": check_channel(ctx, snow(b.get("review_channel_id"), "Review channel"), "Review channel"),
        "panel_channel_id": check_channel(ctx, snow(b.get("panel_channel_id"), "Panel channel"), "Panel channel"),
        "accept_role_ids": jdump(check_roles(ctx, snow_list(b.get("accept_role_ids"), "Accept roles"), "Accept roles")),
        "remove_role_ids": jdump(check_roles(ctx, snow_list(b.get("remove_role_ids"), "Remove roles"), "Remove roles")),
        "reviewer_role_ids": jdump(check_roles(ctx, snow_list(b.get("reviewer_role_ids"), "Reviewer roles"), "Reviewer roles")),
        "cooldown_hours": i_(b.get("cooldown_hours"), "Cooldown", 0, 8760, 24),
        "max_submissions": i_(b.get("max_submissions"), "Max submissions", 0, 1000, 0),
        "required_votes": i_(b.get("required_votes"), "Required votes", 1, 25, 1),
        "accept_message": s_(b.get("accept_message") or "Congratulations! Your {form} application in {server} was accepted.", "Accept message", 2000),
        "deny_message": s_(b.get("deny_message") or "Thank you for applying. Your {form} application in {server} was not accepted this time.", "Deny message", 2000),
        "enabled": int(bool(b.get("enabled", True))),
    }


@router.get("/application-forms")
async def list_forms(request: Request, gid: int):
    ctx = await guild_ctx(request, gid)
    rows = await ctx.bot.db.fetchall(
        "SELECT f.*, (SELECT COUNT(*) FROM application_submissions s WHERE s.form_id=f.id) AS submissions, "
        "(SELECT COUNT(*) FROM application_submissions s WHERE s.form_id=f.id AND s.status='pending') AS pending, "
        "(SELECT COUNT(*) FROM application_submissions s WHERE s.form_id=f.id AND s.status='accepted') AS accepted "
        "FROM application_forms f WHERE f.guild_id=? ORDER BY f.id", (gid,))
    return {"forms": [decode(r, "questions", "accept_role_ids", "remove_role_ids", "reviewer_role_ids") for r in rows]}


@router.post("/application-forms")
async def create_form(request: Request, gid: int):
    ctx = await guild_ctx(request, gid, write=True)
    data = form_body(ctx, await request.json())
    fid = await ctx.bot.db.execute(f"INSERT INTO application_forms (guild_id, {', '.join(data)}) VALUES (?, {','.join('?' * len(data))})", (gid, *data.values()))
    await audit(ctx, "applications.form.create", {"id": fid, "name": data["name"]})
    return {"ok": True, "id": fid}


@router.put("/application-forms/{fid}")
async def update_form(request: Request, gid: int, fid: int):
    ctx = await guild_ctx(request, gid, write=True)
    data = form_body(ctx, await request.json())
    n = await ctx.bot.db.execute_rowcount(f"UPDATE application_forms SET {', '.join(f'{k}=?' for k in data)} WHERE id=? AND guild_id=?", (*data.values(), fid, gid))
    if not n:
        raise HTTPException(404, "Form not found")
    await audit(ctx, "applications.form.update", {"id": fid})
    return {"ok": True}


@router.delete("/application-forms/{fid}")
async def delete_form(request: Request, gid: int, fid: int):
    ctx = await guild_ctx(request, gid, write=True)
    n = await ctx.bot.db.execute_rowcount("DELETE FROM application_forms WHERE id=? AND guild_id=?", (fid, gid))
    if not n:
        raise HTTPException(404, "Form not found")
    await audit(ctx, "applications.form.delete", {"id": fid})
    return {"ok": True}


@router.post("/application-forms/{fid}/publish")
async def publish_form(request: Request, gid: int, fid: int):
    ctx = await guild_ctx(request, gid, write=True)
    if not await ctx.bot.db.fetchval("SELECT 1 FROM application_forms WHERE id=? AND guild_id=?", (fid, gid)):
        raise HTTPException(404, "Form not found")
    try:
        msg = await need_cog(ctx, "Applications").publish_form(fid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except discord.Forbidden:
        raise HTTPException(403, "Nexora can't send messages in that channel") from None
    await audit(ctx, "applications.form.publish", {"id": fid})
    return {"ok": True, "url": msg.jump_url}


@router.get("/applications")
async def list_submissions(request: Request, gid: int, status: str = "pending", form_id: int | None = None):
    ctx = await guild_ctx(request, gid)
    rows = await ctx.bot.db.fetchall(
        "SELECT s.id, s.form_id, s.user_id, s.status, s.created_at, s.decided_at, s.reviewer_id, f.name AS form, "
        "(SELECT COUNT(*) FROM application_votes v WHERE v.submission_id=s.id AND v.vote='accept') AS votes_accept, "
        "(SELECT COUNT(*) FROM application_votes v WHERE v.submission_id=s.id AND v.vote='deny') AS votes_deny "
        "FROM application_submissions s JOIN application_forms f ON f.id=s.form_id "
        "WHERE s.guild_id=? AND (?='all' OR s.status=?) AND (? IS NULL OR s.form_id=?) ORDER BY s.id DESC LIMIT 200",
        (gid, status, status, form_id, form_id))
    for r in rows:
        r["user"] = _user_label(ctx.guild, r["user_id"])
    stats = await ctx.bot.db.fetchone(
        "SELECT COUNT(*) total, SUM(status='pending') pending, SUM(status='accepted') accepted, SUM(status='denied') denied, "
        "ROUND(AVG(CASE WHEN decided_at IS NOT NULL THEN (julianday(decided_at)-julianday(created_at))*24 END),1) avg_hours "
        "FROM application_submissions WHERE guild_id=?", (gid,))
    return {"submissions": rows, "stats": stats}


@router.get("/applications/{sid}")
async def submission_detail(request: Request, gid: int, sid: int):
    ctx = await guild_ctx(request, gid)
    db = ctx.bot.db
    s = await db.fetchone("SELECT s.*, f.name AS form FROM application_submissions s JOIN application_forms f ON f.id=s.form_id WHERE s.id=? AND s.guild_id=?", (sid, gid))
    if not s:
        raise HTTPException(404, "Submission not found")
    s["answers"] = jload(s["answers"], [])
    s["user"] = _user_label(ctx.guild, s["user_id"])
    s["reviewer"] = _user_label(ctx.guild, s["reviewer_id"])
    s["votes"] = [{**v, "reviewer": _user_label(ctx.guild, v["reviewer_id"])} for v in await db.fetchall("SELECT * FROM application_votes WHERE submission_id=?", (sid,))]
    s["notes"] = [{**n, "reviewer": _user_label(ctx.guild, n["reviewer_id"])} for n in await db.fetchall("SELECT * FROM application_notes WHERE submission_id=? ORDER BY id", (sid,))]
    s["history"] = await db.fetchall("SELECT s.id, f.name AS form, s.status, s.created_at FROM application_submissions s JOIN application_forms f ON f.id=s.form_id WHERE s.guild_id=? AND s.user_id=? AND s.id != ? ORDER BY s.id DESC LIMIT 10", (gid, s["user_id"], sid))
    return s


async def _reviewer(ctx, sid: int):
    s = await ctx.bot.db.fetchone("SELECT * FROM application_submissions WHERE id=? AND guild_id=?", (sid, ctx.gid))
    if not s:
        raise HTTPException(404, "Submission not found")
    form = await ctx.bot.db.fetchone("SELECT * FROM application_forms WHERE id=?", (s["form_id"],))
    member = ctx.guild.get_member(ctx.user_id)
    cog = need_cog(ctx, "Applications")
    if not member or not await cog.can_review(member, form):
        raise HTTPException(403, "You aren't a reviewer for this form")
    return cog, member


@router.post("/applications/{sid}/vote")
async def vote_submission(request: Request, gid: int, sid: int):
    ctx = await guild_ctx(request, gid, write=True)
    cog, member = await _reviewer(ctx, sid)
    b = await request.json()
    msg = await cog.vote(ctx.guild, sid, member, bool(b.get("accept")), s_(b.get("reason"), "Reason", 1000) or None)
    await audit(ctx, "applications.vote", {"id": sid, "accept": bool(b.get("accept"))})
    return {"ok": True, "message": msg}


@router.post("/applications/{sid}/notes")
async def note_submission(request: Request, gid: int, sid: int):
    ctx = await guild_ctx(request, gid, write=True)
    cog, member = await _reviewer(ctx, sid)
    await cog.add_note(sid, member, s_((await request.json()).get("note"), "Note", 1000, True))
    return {"ok": True}


# ================================================================ giveaways
@router.get("/giveaways")
async def list_giveaways(request: Request, gid: int, status: str = "all"):
    ctx = await guild_ctx(request, gid)
    rows = await ctx.bot.db.fetchall(
        "SELECT g.*, (SELECT COUNT(*) FROM giveaway_entries e WHERE e.giveaway_id=g.id) AS entry_count, "
        "(SELECT COALESCE(SUM(entries),0) FROM giveaway_entries e WHERE e.giveaway_id=g.id) AS ticket_count "
        "FROM giveaways g WHERE g.guild_id=? AND (?='all' OR g.status=?) ORDER BY g.id DESC LIMIT 200", (gid, status, status))
    for r in rows:
        decode(r, "required_role_ids", "winners")
        r["bonus_roles"] = jload(r["bonus_roles"], {})
        r["winners"] = [_user_label(ctx.guild, w) or {"id": str(w)} for w in r["winners"]]
        ch = ctx.guild.get_channel(r["channel_id"])
        r["channel_name"] = ch.name if ch else None
    stats = await ctx.bot.db.fetchone(
        "SELECT COUNT(*) total, SUM(status='running') running, SUM(status='scheduled') scheduled, SUM(status='ended') ended FROM giveaways WHERE guild_id=?", (gid,))
    stats["entries"] = await ctx.bot.db.fetchval("SELECT COUNT(*) FROM giveaway_entries e JOIN giveaways g ON g.id=e.giveaway_id WHERE g.guild_id=?", (gid,), 0)
    return {"giveaways": rows, "stats": stats}


@router.post("/giveaways")
async def create_giveaway(request: Request, gid: int):
    ctx = await guild_ctx(request, gid, write=True)
    b = await request.json()
    channel_id = check_channel(ctx, snow(b.get("channel_id"), "Channel", True))
    d = parse_duration(str(b.get("duration") or ""))
    if not d or d < timedelta(seconds=30) or d > timedelta(days=60):
        raise HTTPException(422, "Duration must be between 30s and 60d (e.g. 2d, 12h)")
    starts = None
    if b.get("starts_at"):
        try:
            starts = datetime.fromisoformat(str(b["starts_at"]).replace("Z", "+00:00"))
            if starts.tzinfo is None:
                starts = starts.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(422, "Invalid start time") from None
    bonus = {}
    for row in (b.get("bonus_roles") or [])[:10]:
        rid = snow(row.get("role_id"), "Bonus role", True)
        check_roles(ctx, [rid], "Bonus role")
        bonus[rid] = i_(row.get("entries"), "Bonus entries", 1, 100)
    gwid = await need_cog(ctx, "Giveaways").create(
        ctx.guild, channel_id, ctx.user_id, s_(b.get("prize"), "Prize", 200, True), d, i_(b.get("winners"), "Winners", 1, 50, 1),
        s_(b.get("description"), "Description", 2000), check_roles(ctx, snow_list(b.get("required_role_ids"), "Required roles"), "Required roles"),
        bonus, i_(b.get("min_account_age_days"), "Min account age", 0, 3650, 0), i_(b.get("min_level"), "Min level", 0, 1000, 0), starts)
    await audit(ctx, "giveaways.create", {"id": gwid})
    return {"ok": True, "id": gwid}


@router.post("/giveaways/{gwid}/{action}")
async def giveaway_action(request: Request, gid: int, gwid: int, action: str):
    ctx = await guild_ctx(request, gid, write=True)
    if not await ctx.bot.db.fetchval("SELECT 1 FROM giveaways WHERE id=? AND guild_id=?", (gwid, gid)):
        raise HTTPException(404, "Giveaway not found")
    cog = need_cog(ctx, "Giveaways")
    b = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    if action == "end":
        result = await cog.end(gwid)
        msg = f"Ended with {len(result)} winner(s)"
    elif action == "reroll":
        result = await cog.reroll(gwid, i_(b.get("count"), "Count", 1, 20, 1))
        msg = f"Rerolled {len(result)} winner(s)" if result else "No eligible entrants left (or giveaway not ended)"
    elif action == "cancel":
        msg = "Cancelled" if await cog.cancel(gwid) else "Can't cancel this giveaway"
    elif action == "extend":
        d = parse_duration(str(b.get("by") or ""))
        if not d:
            raise HTTPException(422, "Give a duration like 1h")
        msg = "Extended" if await cog.extend(gwid, d) else "Can't extend this giveaway"
    else:
        raise HTTPException(404, "Unknown action")
    await audit(ctx, f"giveaways.{action}", {"id": gwid})
    return {"ok": True, "message": msg}


# =============================================================== role menus
def menu_body(ctx, b: dict) -> dict:
    kind = b.get("kind", "button")
    if kind not in {"button", "dropdown", "reaction"}:
        raise HTTPException(422, "Invalid menu type")
    opts = []
    for o in (b.get("options") or [])[:25]:
        rid = snow(o.get("role_id"), "Role", True)
        role = ctx.guild.get_role(rid)
        if not role:
            raise HTTPException(422, "A role in this menu no longer exists")
        if role.managed or (ctx.guild.me and role >= ctx.guild.me.top_role):
            raise HTTPException(422, f"Nexora can't assign @{role.name} (it's above Nexora's role or managed)")
        emoji = s_(o.get("emoji"), "Emoji", 64)
        if kind == "reaction" and not emoji:
            raise HTTPException(422, "Reaction role menus need an emoji for every role")
        opts.append({"role_id": str(rid), "label": s_(o.get("label"), "Label", 80), "emoji": emoji, "description": s_(o.get("description"), "Description", 100)})
    if not opts:
        raise HTTPException(422, "Add at least one role")
    return {"channel_id": check_channel(ctx, snow(b.get("channel_id"), "Channel", True)), "kind": kind,
            "title": s_(b.get("title"), "Title", 256, True), "description": s_(b.get("description"), "Description", 2000),
            "options": jdump(opts), "max_values": i_(b.get("max_values"), "Max roles", 0, 25, 0)}


@router.get("/role-menus")
async def list_menus(request: Request, gid: int):
    ctx = await guild_ctx(request, gid)
    return {"menus": [decode(r, "options") for r in await ctx.bot.db.fetchall("SELECT * FROM role_menus WHERE guild_id=? ORDER BY id", (gid,))]}


@router.post("/role-menus")
async def create_menu(request: Request, gid: int):
    ctx = await guild_ctx(request, gid, write=True)
    data = menu_body(ctx, await request.json())
    mid = await ctx.bot.db.execute(f"INSERT INTO role_menus (guild_id, {', '.join(data)}) VALUES (?, {','.join('?' * len(data))})", (gid, *data.values()))
    await audit(ctx, "rolemenu.create", {"id": mid})
    return {"ok": True, "id": mid}


@router.put("/role-menus/{mid}")
async def update_menu(request: Request, gid: int, mid: int):
    ctx = await guild_ctx(request, gid, write=True)
    data = menu_body(ctx, await request.json())
    old = await ctx.bot.db.fetchone("SELECT channel_id, kind FROM role_menus WHERE id=? AND guild_id=?", (mid, gid))
    if not old:
        raise HTTPException(404, "Menu not found")
    reset_msg = ", message_id=NULL" if old["channel_id"] != data["channel_id"] or old["kind"] != data["kind"] else ""
    await ctx.bot.db.execute(f"UPDATE role_menus SET {', '.join(f'{k}=?' for k in data)}{reset_msg} WHERE id=?", (*data.values(), mid))
    await audit(ctx, "rolemenu.update", {"id": mid})
    return {"ok": True}


@router.delete("/role-menus/{mid}")
async def delete_menu(request: Request, gid: int, mid: int):
    ctx = await guild_ctx(request, gid, write=True)
    menu = await ctx.bot.db.fetchone("SELECT * FROM role_menus WHERE id=? AND guild_id=?", (mid, gid))
    if not menu:
        raise HTTPException(404, "Menu not found")
    await ctx.bot.db.execute("DELETE FROM role_menus WHERE id=?", (mid,))
    ch = ctx.guild.get_channel(menu["channel_id"] or 0)
    if isinstance(ch, discord.TextChannel) and menu["message_id"]:
        try:
            await ch.get_partial_message(menu["message_id"]).delete()
        except discord.HTTPException:
            pass
    await audit(ctx, "rolemenu.delete", {"id": mid})
    return {"ok": True}


@router.post("/role-menus/{mid}/publish")
async def publish_menu(request: Request, gid: int, mid: int):
    ctx = await guild_ctx(request, gid, write=True)
    if not await ctx.bot.db.fetchval("SELECT 1 FROM role_menus WHERE id=? AND guild_id=?", (mid, gid)):
        raise HTTPException(404, "Menu not found")
    try:
        msg = await need_cog(ctx, "Roles").publish_menu(mid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except discord.Forbidden:
        raise HTTPException(403, "Nexora can't send messages in that channel") from None
    await audit(ctx, "rolemenu.publish", {"id": mid})
    return {"ok": True, "url": msg.jump_url}


# ============================================================ simple CRUDs
def shop_body(ctx, b):
    rid = snow(b.get("role_id"), "Role")
    if rid:
        check_roles(ctx, [rid], "Role")
    return {"name": s_(b.get("name"), "Name", 80, True), "description": s_(b.get("description"), "Description", 300),
            "price": i_(b.get("price"), "Price", 0, 1_000_000_000), "role_id": rid, "stock": i_(b.get("stock"), "Stock", -1, 1_000_000, -1),
            "enabled": int(bool(b.get("enabled", True)))}


def command_body(ctx, b):
    name = s_(b.get("name"), "Name", 32, True).lower().lstrip("!")
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
        raise HTTPException(422, "Command names can only use letters, numbers, - and _")
    embed = None
    if b.get("embed") and any((b["embed"] or {}).get(k) for k in ("title", "description", "fields", "image")):
        e = dict(b["embed"])
        e.setdefault("content", "")
        embed = jdump(validate_embed(e))
    response = s_(b.get("response"), "Response", 2000)
    if not response and not embed:
        raise HTTPException(422, "Give the command a response or an embed")
    return {"name": name, "response": response, "embed": embed, "delete_trigger": int(bool(b.get("delete_trigger"))),
            "required_role_ids": jdump(check_roles(ctx, snow_list(b.get("required_role_ids"), "Roles"), "Roles")), "enabled": int(bool(b.get("enabled", True)))}


def scheduled_body(ctx, b):
    try:
        when = datetime.fromisoformat(str(b.get("next_run_at") or "").replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "Choose a valid date and time") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    embed = jdump(validate_embed({**b.get("embed"), "content": b.get("content", "")})) if b.get("use_embed") and b.get("embed") else None
    content = s_(b.get("content"), "Content", 2000)
    if not content and not embed:
        raise HTTPException(422, "Message is empty")
    return {"channel_id": check_channel(ctx, snow(b.get("channel_id"), "Channel", True)), "content": content, "embed": embed,
            "next_run_at": to_db_time(when), "repeat_hours": i_(b.get("repeat_hours"), "Repeat", 0, 8760, 0), "enabled": int(bool(b.get("enabled", True)))}


def embed_save_body(ctx, b):
    return {"name": s_(b.get("name"), "Name", 64, True), "data": jdump(validate_embed(b.get("data") or {}))}


CRUD = {
    "shop": ("shop_items", shop_body, "SELECT * FROM shop_items WHERE guild_id=? ORDER BY price", ()),
    "commands": ("custom_commands", command_body, "SELECT * FROM custom_commands WHERE guild_id=? ORDER BY name", ("required_role_ids",)),
    "scheduled": ("scheduled_messages", scheduled_body, "SELECT * FROM scheduled_messages WHERE guild_id=? ORDER BY next_run_at", ()),
    "embeds": ("saved_embeds", embed_save_body, "SELECT * FROM saved_embeds WHERE guild_id=? ORDER BY name", ()),
}


def _crud(kind: str):
    if kind not in CRUD:
        raise HTTPException(404, "Not found")
    return CRUD[kind]


def _out(kind: str, r: dict) -> dict:
    for c in CRUD[kind][3]:
        r[c] = jload(r[c], [])
    for c in ("embed", "data"):
        if c in r:
            r[c] = jload(r[c], None)
    return r


@router.get("/collections/{kind}")
async def crud_list(request: Request, gid: int, kind: str):
    ctx = await guild_ctx(request, gid)
    table, _, sql, _ = _crud(kind)
    return {"items": [_out(kind, r) for r in await ctx.bot.db.fetchall(sql, (gid,))]}


@router.post("/collections/{kind}")
async def crud_create(request: Request, gid: int, kind: str):
    ctx = await guild_ctx(request, gid, write=True)
    table, body_fn, _, _ = _crud(kind)
    if await ctx.bot.db.fetchval(f"SELECT COUNT(*) FROM {table} WHERE guild_id=?", (gid,), 0) >= 200:
        raise HTTPException(422, "Limit of 200 items reached")
    data = body_fn(ctx, await request.json())
    if kind == "scheduled":
        data["created_by"] = ctx.user_id
    try:
        new_id = await ctx.bot.db.execute(f"INSERT INTO {table} (guild_id, {', '.join(data)}) VALUES (?, {','.join('?' * len(data))})", (gid, *data.values()))
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(422, "An item with that name already exists") from None
        raise
    await audit(ctx, f"{kind}.create", {"id": new_id})
    return {"ok": True, "id": new_id}


@router.put("/collections/{kind}/{item_id}")
async def crud_update(request: Request, gid: int, kind: str, item_id: int):
    ctx = await guild_ctx(request, gid, write=True)
    table, body_fn, _, _ = _crud(kind)
    data = body_fn(ctx, await request.json())
    try:
        n = await ctx.bot.db.execute_rowcount(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in data)} WHERE id=? AND guild_id=?", (*data.values(), item_id, gid))
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(422, "An item with that name already exists") from None
        raise
    if not n:
        raise HTTPException(404, "Item not found")
    await audit(ctx, f"{kind}.update", {"id": item_id})
    return {"ok": True}


@router.delete("/collections/{kind}/{item_id}")
async def crud_delete(request: Request, gid: int, kind: str, item_id: int):
    ctx = await guild_ctx(request, gid, write=True)
    table = _crud(kind)[0]
    n = await ctx.bot.db.execute_rowcount(f"DELETE FROM {table} WHERE id=? AND guild_id=?", (item_id, gid))
    if not n:
        raise HTTPException(404, "Item not found")
    await audit(ctx, f"{kind}.delete", {"id": item_id})
    return {"ok": True}


@router.post("/embeds/send")
async def send_embed(request: Request, gid: int):
    ctx = await guild_ctx(request, gid, write=True)
    b = await request.json()
    cid = check_channel(ctx, snow(b.get("channel_id"), "Channel", True))
    data = validate_embed(b.get("data") or {})
    member = ctx.guild.get_member(ctx.user_id)
    ch = ctx.guild.get_channel(cid)
    if not member or not ch.permissions_for(member).send_messages:
        raise HTTPException(403, "You can't send messages in that channel")
    try:
        msg = await need_cog(ctx, "Utility").send_payload(ch, data["content"], data, ctx.guild)
    except discord.Forbidden:
        raise HTTPException(403, "Nexora can't send messages in that channel") from None
    except discord.HTTPException as exc:
        raise HTTPException(422, f"Discord rejected the message: {exc.text}") from None
    await audit(ctx, "embeds.send", {"channel_id": cid})
    return {"ok": True, "url": msg.jump_url}


# ============================================================== suggestions
@router.get("/suggestions")
async def list_suggestions(request: Request, gid: int, status: str = "all"):
    ctx = await guild_ctx(request, gid)
    rows = await ctx.bot.db.fetchall(
        "SELECT s.*, (SELECT COUNT(*) FROM suggestion_votes v WHERE v.suggestion_id=s.id AND v.vote>0) up, "
        "(SELECT COUNT(*) FROM suggestion_votes v WHERE v.suggestion_id=s.id AND v.vote<0) down FROM suggestions s "
        "WHERE s.guild_id=? AND (?='all' OR s.status=?) ORDER BY s.id DESC LIMIT 200", (gid, status, status))
    for r in rows:
        r["user"] = _user_label(ctx.guild, r["user_id"])
    return {"suggestions": rows}


@router.post("/suggestions/{number}/respond")
async def respond_suggestion(request: Request, gid: int, number: int):
    ctx = await guild_ctx(request, gid, write=True)
    b = await request.json()
    from nexora.bot.cogs.community import SUGGESTION_STATUS
    if b.get("status") not in SUGGESTION_STATUS:
        raise HTTPException(422, "Invalid status")
    msg = await need_cog(ctx, "Community").respond_suggestion(ctx.guild, number, b["status"], s_(b.get("response"), "Response", 1000) or None,
                                                             ctx.guild.get_member(ctx.user_id))
    await audit(ctx, "suggestions.respond", {"number": number, "status": b["status"]})
    return {"ok": True, "message": msg}


# ==================================================================== polls
@router.get("/polls")
async def list_polls(request: Request, gid: int):
    ctx = await guild_ctx(request, gid)
    rows = await ctx.bot.db.fetchall("SELECT * FROM polls WHERE guild_id=? ORDER BY id DESC LIMIT 100", (gid,))
    for r in rows:
        r["options"] = jload(r["options"], [])
        counts = {c["option_index"]: c["n"] for c in await ctx.bot.db.fetchall("SELECT option_index, COUNT(*) n FROM poll_votes WHERE poll_id=? GROUP BY option_index", (r["id"],))}
        r["results"] = [counts.get(i, 0) for i in range(len(r["options"]))]
    return {"polls": rows}


@router.post("/polls")
async def create_poll(request: Request, gid: int):
    ctx = await guild_ctx(request, gid, write=True)
    b = await request.json()
    ch = ctx.guild.get_channel(check_channel(ctx, snow(b.get("channel_id"), "Channel", True)))
    opts = [s_(o, "Option", 80) for o in (b.get("options") or []) if str(o).strip()]
    if not 2 <= len(opts) <= 20:
        raise HTTPException(422, "Polls need 2-20 options")
    d = parse_duration(str(b.get("duration") or "")) if b.get("duration") else None
    pid = await need_cog(ctx, "Community").create_poll(ctx.guild, ch, ctx.user_id, s_(b.get("question"), "Question", 250, True), opts, bool(b.get("multi")), d)
    await audit(ctx, "polls.create", {"id": pid})
    return {"ok": True, "id": pid}


@router.post("/polls/{pid}/end")
async def end_poll(request: Request, gid: int, pid: int):
    ctx = await guild_ctx(request, gid, write=True)
    poll = await ctx.bot.db.fetchone("SELECT * FROM polls WHERE id=? AND guild_id=?", (pid, gid))
    if not poll:
        raise HTTPException(404, "Poll not found")
    await ctx.bot.db.execute("UPDATE polls SET status='closed' WHERE id=?", (pid,))
    await need_cog(ctx, "Community").refresh_poll(poll)
    await audit(ctx, "polls.end", {"id": pid})
    return {"ok": True}


# ============================================================== automations
@router.get("/automations")
async def list_automations(request: Request, gid: int):
    ctx = await guild_ctx(request, gid)
    rows = await ctx.bot.db.fetchall("SELECT * FROM automations WHERE guild_id=? ORDER BY id", (gid,))
    for r in rows:
        r["conditions"] = jload(r["conditions"], [])
        r["actions"] = jload(r["actions"], [])
        r["errors_24h"] = await ctx.bot.db.fetchval("SELECT COUNT(*) FROM automation_runs WHERE automation_id=? AND status='error' AND created_at > datetime('now','-1 day')", (r["id"],), 0)
    return {"automations": rows}


async def _save_automation(ctx, b, aid: int | None):
    try:
        data = validate_automation(b)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    for c in data["conditions"]:
        if c["type"] in {"has_role", "lacks_role", "role_is"}:
            check_roles(ctx, [c["value"]], "Condition role")
        if c["type"] == "channel_is" and not ctx.guild.get_channel(c["value"]):
            raise HTTPException(422, "Condition channel not found")
    for a in data["actions"]:
        if "channel" in a:
            check_channel(ctx, a["channel"], "Action channel")
        if "role" in a:
            check_roles(ctx, [a["role"]], "Action role")
        if "panel" in a and not await ctx.bot.db.fetchval("SELECT 1 FROM ticket_panels WHERE id=? AND guild_id=?", (a["panel"], ctx.gid)):
            raise HTTPException(422, "Ticket panel not found")
    vals = (data["name"], data["trigger"], jdump(data["conditions"]), jdump(data["actions"]), int(data["enabled"]))
    if aid is None:
        if await ctx.bot.db.fetchval("SELECT COUNT(*) FROM automations WHERE guild_id=?", (ctx.gid,), 0) >= 100:
            raise HTTPException(422, "Limit of 100 automations reached")
        aid = await ctx.bot.db.execute("INSERT INTO automations (guild_id, name, trigger, conditions, actions, enabled) VALUES (?, ?, ?, ?, ?, ?)", (ctx.gid, *vals))
    else:
        n = await ctx.bot.db.execute_rowcount("UPDATE automations SET name=?, trigger=?, conditions=?, actions=?, enabled=? WHERE id=? AND guild_id=?", (*vals, aid, ctx.gid))
        if not n:
            raise HTTPException(404, "Automation not found")
    cog = ctx.bot.get_cog("Automation")
    if cog:
        cog.invalidate(ctx.gid)
    return aid


@router.post("/automations")
async def create_automation(request: Request, gid: int):
    ctx = await guild_ctx(request, gid, write=True)
    aid = await _save_automation(ctx, await request.json(), None)
    await audit(ctx, "automation.create", {"id": aid})
    return {"ok": True, "id": aid}


@router.put("/automations/{aid}")
async def update_automation(request: Request, gid: int, aid: int):
    ctx = await guild_ctx(request, gid, write=True)
    await _save_automation(ctx, await request.json(), aid)
    await audit(ctx, "automation.update", {"id": aid})
    return {"ok": True}


@router.post("/automations/{aid}/toggle")
async def toggle_automation(request: Request, gid: int, aid: int):
    ctx = await guild_ctx(request, gid, write=True)
    n = await ctx.bot.db.execute_rowcount("UPDATE automations SET enabled = 1 - enabled WHERE id=? AND guild_id=?", (aid, gid))
    if not n:
        raise HTTPException(404, "Automation not found")
    cog = ctx.bot.get_cog("Automation")
    if cog:
        cog.invalidate(gid)
    await audit(ctx, "automation.toggle", {"id": aid})
    return {"ok": True}


@router.delete("/automations/{aid}")
async def delete_automation(request: Request, gid: int, aid: int):
    ctx = await guild_ctx(request, gid, write=True)
    n = await ctx.bot.db.execute_rowcount("DELETE FROM automations WHERE id=? AND guild_id=?", (aid, gid))
    if not n:
        raise HTTPException(404, "Automation not found")
    await ctx.bot.db.execute("DELETE FROM automation_runs WHERE automation_id=?", (aid,))
    cog = ctx.bot.get_cog("Automation")
    if cog:
        cog.invalidate(gid)
    await audit(ctx, "automation.delete", {"id": aid})
    return {"ok": True}


@router.get("/automations/{aid}/runs")
async def automation_runs(request: Request, gid: int, aid: int):
    ctx = await guild_ctx(request, gid)
    return {"runs": await ctx.bot.db.fetchall("SELECT * FROM automation_runs WHERE automation_id=? AND guild_id=? ORDER BY id DESC LIMIT 100", (aid, gid))}


# ============================================================== leaderboard
@router.get("/leaderboard")
async def leaderboard(request: Request, gid: int, board: str = "xp", limit: int = 100):
    ctx = await guild_ctx(request, gid)
    return await leaderboard_data(ctx.bot, ctx.guild, board, limit)


async def leaderboard_data(bot, guild, board: str, limit: int) -> dict:
    limit = max(1, min(limit, 200))
    if board == "economy":
        rows = await bot.db.fetchall("SELECT user_id, balance AS value FROM economy WHERE guild_id=? ORDER BY balance DESC LIMIT ?", (guild.id, limit))
    else:
        rows = await bot.db.fetchall("SELECT user_id, xp AS value, level, messages FROM levels WHERE guild_id=? ORDER BY xp DESC LIMIT ?", (guild.id, limit))
    for r in rows:
        r["user"] = _user_label(guild, r["user_id"])
    return {"board": board, "rows": rows}
