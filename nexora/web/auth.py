"""Discord OAuth2 login + per-guild access control for the dashboard."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlparse

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from nexora.web.security import SESSION_COOKIE, SESSION_TTL, Session, check_csrf

if TYPE_CHECKING:
    import discord

DISCORD_API = "https://discord.com/api/v10"
MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x8
GUILD_CACHE_SECONDS = 300

router = APIRouter()


def state(request: Request):
    return request.app.state


async def discord_get(path: str, token: str) -> Any:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(f"{DISCORD_API}{path}", headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status == 401:
                raise HTTPException(401, "Discord session expired, please log in again")
            if r.status == 429:
                raise HTTPException(429, "Discord is rate limiting us, try again in a moment")
            r.raise_for_status()
            return await r.json()


@router.get("/auth/login")
async def login(request: Request, next: str = "/dashboard"):
    cfg = state(request).config
    if not cfg.oauth_enabled:
        raise HTTPException(503, "Discord login isn't configured. Set DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET in .env")
    if not next.startswith("/") or next.startswith("//"):
        next = "/dashboard"
    # The login check cookie must be set on the same address Discord sends the user back to.
    callback = urlparse(cfg.redirect_uri)
    if callback.netloc and request.url.netloc != callback.netloc:
        return RedirectResponse(f"{callback.scheme}://{callback.netloc}/auth/login?{urlencode({'next': next})}")
    nonce = secrets.token_urlsafe(24)
    signer = URLSafeTimedSerializer(cfg.session_secret, salt="oauth-state")
    params = {
        "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri, "response_type": "code",
        "scope": "identify guilds", "state": nonce, "prompt": "none",
    }
    resp = RedirectResponse(f"https://discord.com/oauth2/authorize?{urlencode(params)}")
    resp.set_cookie("nx_oauth", signer.dumps({"n": nonce, "next": next}), max_age=600, httponly=True,
                    samesite="lax", secure=cfg.secure_cookies)
    return resp


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", state_param: str = "", error: str = ""):
    st = state(request)
    cfg = st.config
    state_value = request.query_params.get("state", state_param)
    if error:
        return RedirectResponse("/?login_error=" + error[:50])
    signer = URLSafeTimedSerializer(cfg.session_secret, salt="oauth-state")
    try:
        data = signer.loads(request.cookies.get("nx_oauth", ""), max_age=600)
    except BadSignature:
        raise HTTPException(400, "Login expired or invalid. Please try again.") from None
    if not code or not secrets.compare_digest(str(data.get("n")), state_value):
        raise HTTPException(400, "Invalid OAuth state")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post(f"{DISCORD_API}/oauth2/token", data={
            "client_id": cfg.client_id, "client_secret": cfg.client_secret, "grant_type": "authorization_code",
            "code": code, "redirect_uri": cfg.redirect_uri,
        }) as r:
            if r.status != 200:
                raise HTTPException(400, "Discord rejected the login. Check DISCORD_REDIRECT_URI matches the Developer Portal exactly.")
            token = (await r.json())["access_token"]
    user = await discord_get("/users/@me", token)
    session = await st.sessions.create({k: user.get(k) for k in ("id", "username", "global_name", "avatar")}, token)
    resp = RedirectResponse(data.get("next") or "/dashboard")
    resp.delete_cookie("nx_oauth")
    resp.set_cookie(SESSION_COOKIE, session.id, max_age=SESSION_TTL, httponly=True, samesite="lax", secure=cfg.secure_cookies)
    return resp


@router.post("/auth/logout")
async def logout(request: Request):
    session = await get_session(request)
    if session:
        token = request.headers.get("X-CSRF-Token") or (await request.form()).get("csrf", "")
        if not secrets.compare_digest(str(token), session.csrf_token):
            raise HTTPException(403, "Invalid CSRF token")
        await state(request).sessions.delete(session.id)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# --------------------------------------------------------------- access helpers
async def get_session(request: Request) -> Session | None:
    if hasattr(request.state, "nx_session"):
        return request.state.nx_session
    sess = await state(request).sessions.get(request.cookies.get(SESSION_COOKIE))
    request.state.nx_session = sess
    return sess


async def require_session(request: Request) -> Session:
    sess = await get_session(request)
    if not sess:
        raise HTTPException(401, "Not logged in")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not check_csrf(request, sess):
        raise HTTPException(403, "Invalid CSRF token. Refresh the page and try again.")
    return sess


async def oauth_guilds(request: Request, sess: Session, force: bool = False) -> list[dict]:
    if sess.guilds is not None and not force and sess.guilds_fetched_at and time.time() - sess.guilds_fetched_at < GUILD_CACHE_SECONDS:
        return sess.guilds
    guilds = await discord_get("/users/@me/guilds", sess.access_token)
    slim = [{"id": g["id"], "name": g["name"], "icon": g.get("icon"), "owner": g.get("owner", False),
             "permissions": int(g.get("permissions", 0))} for g in guilds]
    await state(request).sessions.save_guilds(sess.id, slim)
    sess.guilds, sess.guilds_fetched_at = slim, time.time()
    return slim


@dataclass
class GuildCtx:
    guild: "discord.Guild"
    session: Session
    level: str  # "manage" | "read"
    bot: Any

    @property
    def gid(self) -> int:
        return self.guild.id

    @property
    def user_id(self) -> int:
        return self.session.user_id


async def access_level(bot, guild, user_id: int) -> str | None:
    """Compute dashboard access from live bot data (always current, unlike cached OAuth lists)."""
    member = guild.get_member(user_id)
    if member is None:
        # Without the Server Members intent the member cache is empty, so ask Discord directly.
        try:
            member = await guild.fetch_member(user_id)
        except Exception:  # not in the server, or Discord unreachable
            return None
    perms = member.guild_permissions
    if guild.owner_id == user_id or perms.administrator or perms.manage_guild:
        return "manage"
    general = await bot.settings.get(guild.id, "general")
    role_ids = {r.id for r in member.roles}
    if role_ids & set(general["dashboard_role_ids"]):
        return "manage"
    if role_ids & set(general["dashboard_readonly_role_ids"]):
        return "read"
    return None


async def guild_ctx(request: Request, gid: int, write: bool = False) -> GuildCtx:
    sess = await require_session(request)
    bot = state(request).bot
    guild = bot.get_guild(gid) if bot else None
    if guild is None:
        raise HTTPException(404, "Nexora isn't in that server (or the bot is offline).")
    level = await access_level(bot, guild, sess.user_id)
    if level is None:
        raise HTTPException(403, "You don't have access to this server's dashboard.")
    if write and level != "manage":
        raise HTTPException(403, "You have read-only access to this dashboard.")
    return GuildCtx(guild, sess, level, bot)


def can_manage_from_oauth(g: dict) -> bool:
    return bool(g.get("owner")) or bool(g["permissions"] & (MANAGE_GUILD | ADMINISTRATOR))
