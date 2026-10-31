"""Web security: server-side sessions, CSRF, rate limiting, security headers."""
from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from nexora.db import Database, jdump, jload

SESSION_COOKIE = "nx_session"
SESSION_TTL = 7 * 86400


@dataclass
class Session:
    id: str
    user: dict
    access_token: str
    csrf_token: str
    guilds: list[dict] | None
    guilds_fetched_at: float | None

    @property
    def user_id(self) -> int:
        return int(self.user["id"])


class SessionStore:
    """Sessions live in the database; the cookie only holds a random 256-bit ID.
    Discord access tokens therefore never reach the browser."""

    def __init__(self, db: Database):
        self.db = db

    async def create(self, user: dict, access_token: str) -> Session:
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        await self.db.execute(
            "INSERT INTO web_sessions (id, user_id, user_json, access_token, csrf_token, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, int(user["id"]), jdump(user), access_token, csrf, time.time() + SESSION_TTL))
        return Session(sid, user, access_token, csrf, None, None)

    async def get(self, sid: str | None) -> Session | None:
        if not sid or len(sid) > 100:
            return None
        row = await self.db.fetchone("SELECT * FROM web_sessions WHERE id=? AND expires_at > ?", (sid, time.time()))
        if not row:
            return None
        return Session(row["id"], jload(row["user_json"], {}), row["access_token"], row["csrf_token"],
                       jload(row["guilds_json"], None), row["guilds_fetched_at"])

    async def save_guilds(self, sid: str, guilds: list[dict]) -> None:
        await self.db.execute("UPDATE web_sessions SET guilds_json=?, guilds_fetched_at=? WHERE id=?", (jdump(guilds), time.time(), sid))

    async def delete(self, sid: str) -> None:
        await self.db.execute("DELETE FROM web_sessions WHERE id=?", (sid,))


class RateLimiter:
    def __init__(self):
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(self.hits) > 50_000:  # keep memory bounded
            for k in list(self.hits)[:10_000]:
                self.hits.pop(k, None)
        return True


class SecurityMiddleware(BaseHTTPMiddleware):
    """Rate limiting for /api and /auth, plus security headers on every response."""

    def __init__(self, app, limiter: RateLimiter, secure_cookies: bool):
        super().__init__(app)
        self.limiter = limiter
        self.secure = secure_cookies

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        ip = request.client.host if request.client else "unknown"
        if path.startswith("/api/") or path.startswith("/auth/"):
            write = request.method not in {"GET", "HEAD", "OPTIONS"}
            limit, window = (40, 60.0) if write else (240, 60.0)
            if path.startswith("/auth/"):
                limit, window = 20, 60.0
            if not self.limiter.allow(f"{ip}:{'w' if write else 'r'}:{path.startswith('/auth/')}", limit, window):
                return JSONResponse({"error": "Too many requests. Slow down."}, status_code=429, headers={"Retry-After": "30"})
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https: ; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com",
        )
        if self.secure:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


def check_csrf(request: Request, session: Session) -> bool:
    token = request.headers.get("X-CSRF-Token", "")
    return bool(token) and secrets.compare_digest(token, session.csrf_token)
