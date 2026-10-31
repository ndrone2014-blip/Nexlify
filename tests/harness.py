"""Test harness.

Builds a *real* discord.py Guild (channels, roles, members) from gateway-style
payloads without connecting to Discord, runs the real website with uvicorn on a
free port, and talks to it over HTTP with aiohttp. Nothing is mocked except the
Discord network itself.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import tempfile
from pathlib import Path

import aiohttp
import discord
import uvicorn

from nexora.bot import EXTENSIONS, NexoraBot
from nexora.config import Config
from nexora.db import Database
from nexora.web.app import create_app
from nexora.web.security import SESSION_COOKIE

GUILD_ID = 900000000000000001
BOT_ID = 900000000000000002
OWNER_ID = 900000000000000010   # server owner, also a bot owner
MOD_ID = 900000000000000011     # has Manage Server via role
READER_ID = 900000000000000012  # read-only dashboard role
NOBODY_ID = 900000000000000013  # regular member, no access

ROLE_ADMIN = 900000000000000100
ROLE_READONLY = 900000000000000101
ROLE_MEMBER = 900000000000000102
ROLE_BOT = 900000000000000103
CH_GENERAL = 900000000000000200
CH_LOGS = 900000000000000201
CAT_TICKETS = 900000000000000202
VC_STATS = 900000000000000203

MANAGE_GUILD_PERMS = discord.Permissions(manage_guild=True, ban_members=True, kick_members=True, moderate_members=True, manage_messages=True)


def _user(uid, name):
    return {"id": str(uid), "username": name, "discriminator": "0", "avatar": None, "global_name": name.title()}


def _role(rid, name, pos, perms=0):
    return {"id": str(rid), "name": name, "position": pos, "permissions": str(perms), "color": 0, "hoist": False, "managed": False, "mentionable": False}


def guild_payload():
    return {
        "id": str(GUILD_ID), "name": "Test Server", "owner_id": str(OWNER_ID), "icon": None, "member_count": 5,
        "features": [], "emojis": [], "stickers": [], "premium_tier": 0, "verification_level": 0, "default_message_notifications": 0,
        "explicit_content_filter": 0, "mfa_level": 0, "nsfw_level": 0, "preferred_locale": "en-US", "system_channel_flags": 0,
        "roles": [
            _role(GUILD_ID, "@everyone", 0, discord.Permissions(view_channel=True, send_messages=True, read_message_history=True).value),
            _role(ROLE_MEMBER, "Member", 1),
            _role(ROLE_READONLY, "Viewer", 2),
            _role(ROLE_ADMIN, "Moderator", 3, MANAGE_GUILD_PERMS.value),
            _role(ROLE_BOT, "Nexora", 10, discord.Permissions.all().value),
        ],
        "channels": [
            {"id": str(CAT_TICKETS), "type": 4, "name": "Tickets", "position": 0, "permission_overwrites": []},
            {"id": str(CH_GENERAL), "type": 0, "name": "general", "position": 1, "permission_overwrites": [], "parent_id": None},
            {"id": str(CH_LOGS), "type": 0, "name": "mod-logs", "position": 2, "permission_overwrites": [], "parent_id": None},
            {"id": str(VC_STATS), "type": 2, "name": "Members: 0", "position": 3, "permission_overwrites": [], "bitrate": 64000, "user_limit": 0},
        ],
        "members": [
            {"user": _user(OWNER_ID, "owner"), "roles": [], "joined_at": "2024-01-01T00:00:00+00:00", "deaf": False, "mute": False, "flags": 0},
            {"user": _user(MOD_ID, "moddy"), "roles": [str(ROLE_ADMIN)], "joined_at": "2024-01-02T00:00:00+00:00", "deaf": False, "mute": False, "flags": 0},
            {"user": _user(READER_ID, "reader"), "roles": [str(ROLE_READONLY)], "joined_at": "2024-01-03T00:00:00+00:00", "deaf": False, "mute": False, "flags": 0},
            {"user": _user(NOBODY_ID, "nobody"), "roles": [str(ROLE_MEMBER)], "joined_at": "2024-01-04T00:00:00+00:00", "deaf": False, "mute": False, "flags": 0},
            {"user": {**_user(BOT_ID, "nexora"), "bot": True}, "roles": [str(ROLE_BOT)], "joined_at": "2024-01-01T00:00:00+00:00", "deaf": False, "mute": False, "flags": 0},
        ],
    }


class Env:
    def __init__(self, ready: bool):
        self.ready = ready
        self.tmp = tempfile.TemporaryDirectory()
        self.cookies: dict[int, tuple[str, str]] = {}

    async def __aenter__(self) -> "Env":
        tmp = Path(self.tmp.name)
        self.config = Config(bot_token="", client_id="123", client_secret="secret", owner_ids=[OWNER_ID],
                             database_path=tmp / "t.db", backup_dir=tmp / "backups", session_secret="x" * 40)
        self.db = Database(self.config.database_path)
        await self.db.connect()
        self.bot = NexoraBot(self.config, self.db)
        await self.bot.__aenter__()
        for ext in EXTENSIONS:
            await self.bot.load_extension(ext)
        state = self.bot._connection
        state.user = discord.ClientUser(state=state, data={**_user(BOT_ID, "nexora"), "bot": True})
        self.guild = discord.Guild(data=guild_payload(), state=state)
        state._add_guild(self.guild)
        if self.ready:
            self.bot._ready.set()
        self.app = create_app(self.config, self.db, self.bot)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        self.server.install_signal_handlers = lambda: None
        self.server_task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.http = aiohttp.ClientSession(base_url=f"http://127.0.0.1:{self.port}")
        return self

    async def __aexit__(self, *exc):
        await self.http.close()
        self.server.should_exit = True
        await self.server_task
        with contextlib.suppress(Exception):
            await self.bot.__aexit__(None, None, None)
        await self.db.close()
        self.tmp.cleanup()

    async def login(self, user_id: int) -> None:
        sess = await self.app.state.sessions.create(_user(user_id, f"user{user_id}"), "fake-token")
        # Pretend the OAuth guild list was already fetched (no network in tests).
        await self.app.state.sessions.save_guilds(sess.id, [{"id": str(GUILD_ID), "name": "Test Server", "icon": None,
                                                             "owner": user_id == OWNER_ID, "permissions": 0x20 if user_id in (OWNER_ID, MOD_ID) else 0}])
        self.cookies[user_id] = (sess.id, sess.csrf_token)

    async def req(self, method: str, path: str, user: int | None = OWNER_ID, json=None, csrf: bool = True, raw: bool = False):
        headers, cookies = {}, {}
        if user is not None:
            if user not in self.cookies:
                await self.login(user)
            sid, token = self.cookies[user]
            cookies[SESSION_COOKIE] = sid
            if csrf:
                headers["X-CSRF-Token"] = token
        if path.startswith("guild/"):
            path = f"/api/guilds/{GUILD_ID}/" + path[len("guild/"):]
        async with self.http.request(method, path, json=json, headers=headers, cookies=cookies, allow_redirects=False) as r:
            body = await r.text()
            if raw:
                return r.status, body, r.headers
            try:
                import json as _json
                data = _json.loads(body) if body else None
            except ValueError:
                data = body
            return r.status, data


def run(coro):
    return asyncio.run(coro)
