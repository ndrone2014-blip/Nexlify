"""FastAPI application: public website, dashboard pages and API."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nexora.web import api_core, api_features, api_moderation, auth
from nexora.web.jsonsafe import SafeJSONResponse
from nexora.web.security import RateLimiter, SecurityMiddleware, SessionStore

log = logging.getLogger("nexora.web")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

FEATURES = [
    ("scroll", "Advanced logging", "Message edits with before and after, deletions with attachments, role and permission diffs, voice, invites, webhooks and server changes. Searchable, filterable and exportable from the dashboard."),
    ("shield", "Moderation suite", "Case numbers, warning escalation, temporary bans, notes, moderator statistics and an appeal system that works even for banned users."),
    ("bot", "Automod", "Spam, raids, invites, scam links, profanity, mention and emoji spam, caps and attachments. Every rule can just flag a message for review, and false positives can be undone in one click."),
    ("ticket", "Tickets", "Unlimited panels with their own questions, staff roles and categories. Claiming, transfers, priorities, HTML transcripts and ratings."),
    ("clipboard", "Applications", "A form builder with short, long and multiple-choice questions, cooldowns, limits, multi-reviewer voting, notes and automatic roles."),
    ("gift", "Giveaways", "Button entry, required and bonus roles, account-age and level requirements, scheduling, rerolls and verified winners."),
    ("chart", "Leveling and economy", "XP with multipliers and role rewards, daily streaks, a role shop, achievements and leaderboards."),
    ("zap", "Automation", "Connect triggers like member joins or level ups to conditions and chains of actions, with run history and error reporting."),
    ("palette", "Embed builder", "Design embeds with a live preview, add link buttons, save them, schedule them or send them straight to a channel."),
    ("activity", "Analytics", "Member growth, activity, moderation, tickets, applications and giveaways over any date range, exportable as CSV."),
]


def create_app(config, db, bot) -> FastAPI:
    app = FastAPI(title="Nexora", docs_url=None, redoc_url=None, openapi_url=None, default_response_class=SafeJSONResponse)
    app.state.config = config
    app.state.db = db
    app.state.bot = bot
    app.state.sessions = SessionStore(db)
    app.state.started_at = time.time()
    app.add_middleware(SecurityMiddleware, limiter=RateLimiter(), secure_cookies=config.secure_cookies)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.include_router(auth.router)
    app.include_router(api_core.router)
    app.include_router(api_moderation.router)
    app.include_router(api_features.router)
    app.include_router(api_features.transcript_router)

    def page(request: Request, name: str, status_code: int = 200, **ctx):
        base = {"request": request, "config": config, "invite_url": config.invite_url, "oauth": config.oauth_enabled}
        return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status_code)

    async def current_user(request: Request):
        return await auth.get_session(request)

    # ------------------------------------------------------------ errors
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/") or request.headers.get("accept", "").startswith("application/json"):
            detail = exc.detail
            body = {"error": detail.get("message"), "errors": detail.get("errors")} if isinstance(detail, dict) else {"error": detail}
            return SafeJSONResponse(body, status_code=exc.status_code)
        if exc.status_code == 401:
            return RedirectResponse(f"/auth/login?next={request.url.path}")
        return page(request, "error.html", exc.status_code, code=exc.status_code, message=exc.detail,
                    user=await current_user(request))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return SafeJSONResponse({"error": "Invalid request", "errors": {".".join(map(str, e["loc"])): e["msg"] for e in exc.errors()}}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        import traceback
        log.exception("Unhandled web error")
        await db.log_error(f"web:{request.method} {request.url.path}", repr(exc), traceback.format_exc())
        if request.url.path.startswith("/api/"):
            return SafeJSONResponse({"error": "Unexpected server error. It was recorded in System → Errors."}, status_code=500)
        return page(request, "error.html", 500, code=500, message="Something went wrong on our side.", user=None)

    # ------------------------------------------------------------- public
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return page(request, "home.html", user=await current_user(request), features=FEATURES[:6],
                    servers=len(bot.guilds) if bot and bot.is_ready() else None)

    @app.get("/features", response_class=HTMLResponse)
    async def features(request: Request):
        return page(request, "features.html", user=await current_user(request), features=FEATURES)

    @app.get("/commands", response_class=HTMLResponse)
    async def commands_page(request: Request):
        return page(request, "commands.html", user=await current_user(request), groups=command_catalog(bot))

    @app.get("/docs", response_class=HTMLResponse)
    async def docs(request: Request):
        return page(request, "docs.html", user=await current_user(request))

    @app.get("/support", response_class=HTMLResponse)
    async def support(request: Request):
        return page(request, "support.html", user=await current_user(request))

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        return page(request, "status.html", user=await current_user(request), status=await status_data())

    async def status_data() -> dict:
        t = time.perf_counter()
        db_ok = True
        try:
            await db.fetchval("SELECT 1")
        except Exception:
            db_ok = False
        ready = bool(bot and bot.is_ready())
        return {
            "bot": ready, "website": True, "database": db_ok, "db_ms": round((time.perf_counter() - t) * 1000, 2),
            "latency_ms": round(bot.latency * 1000) if ready and bot.latency == bot.latency else None,
            "servers": len(bot.guilds) if ready else None, "uptime": int(time.time() - bot.started_at) if ready else None,
            "web_uptime": int(time.time() - app.state.started_at), "shards": bot.shard_count or 1 if ready else None,
        }

    @app.get("/api/status")
    async def status_api():
        return await status_data()

    @app.get("/api/commands")
    async def commands_api():
        return {"groups": command_catalog(bot)}

    @app.get("/invite")
    async def invite():
        if not config.invite_url:
            raise HTTPException(503, "Set DISCORD_CLIENT_ID in .env to enable the invite link.")
        return RedirectResponse(config.invite_url)

    @app.get("/leaderboard/{gid}", response_class=HTMLResponse)
    async def public_leaderboard(request: Request, gid: int, board: str = "xp"):
        guild = bot.get_guild(gid) if bot else None
        if not guild:
            raise HTTPException(404, "Server not found")
        s = await bot.settings.get(gid, "economy" if board == "economy" else "leveling")
        if not s["enabled"]:
            raise HTTPException(404, "This leaderboard isn't enabled")
        data = await api_features.leaderboard_data(bot, guild, "economy" if board == "economy" else "xp", 100)
        return page(request, "leaderboard.html", user=await current_user(request), guild=guild, data=data,
                    board=data["board"], currency=s.get("currency_symbol", ""))

    # ---------------------------------------------------------- dashboard
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_index(request: Request):
        user = await current_user(request)
        if not user:
            if not config.oauth_enabled:
                raise HTTPException(503, "Discord login isn't configured yet. Add DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET to .env and restart.")
            return RedirectResponse("/auth/login?next=/dashboard")
        return page(request, "servers.html", user=user)

    @app.get("/dashboard/{gid}", response_class=HTMLResponse)
    async def dashboard(request: Request, gid: int):
        user = await current_user(request)
        if not user:
            return RedirectResponse(f"/auth/login?next=/dashboard/{gid}")
        await auth.guild_ctx(request, gid)  # access check before serving the app shell
        return page(request, "dashboard.html", user=user, gid=str(gid))

    @app.get("/system", response_class=HTMLResponse)
    async def system_page(request: Request):
        user = await current_user(request)
        if not user:
            return RedirectResponse("/auth/login?next=/system")
        if user.user_id not in config.owner_ids:
            raise HTTPException(403, "Only bot owners listed in BOT_OWNER_IDS can open system tools.")
        return page(request, "system.html", user=user)

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    return app


def command_catalog(bot) -> list[dict]:
    """Build the public command list straight from the registered slash commands, so it never drifts."""
    from discord import app_commands

    if not bot:
        return []
    groups: dict[str, list[dict]] = {}
    for cmd in bot.tree.walk_commands():
        if isinstance(cmd, app_commands.Group):
            continue
        cog = cmd.binding.__class__.__name__ if cmd.binding else "Other"
        perms = cmd.root_parent.default_permissions if cmd.root_parent else cmd.default_permissions
        params = [{"name": p.name, "required": p.required, "description": p.description} for p in cmd.parameters]
        groups.setdefault(cog, []).append({
            "name": cmd.qualified_name, "description": cmd.description, "params": params,
            "permission": ", ".join(n.replace("_", " ").title() for n, v in perms if v) if perms else None,
        })
    order = ["Core", "Moderation", "Automod", "Tickets", "Applications", "Giveaways", "Roles", "Leveling", "Economy", "Community", "Utility", "AI"]
    labels = {"Core": "General", "AI": "AI assistant"}
    return [{"name": labels.get(k, k), "commands": sorted(groups[k], key=lambda c: c["name"])}
            for k in sorted(groups, key=lambda k: order.index(k) if k in order else 99)]
