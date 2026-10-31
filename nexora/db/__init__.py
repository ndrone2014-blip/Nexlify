"""Async database layer.

One shared aiosqlite connection is used by both the bot and the website (they run
in the same process). All queries use parameter binding, never string formatting
of user input, which prevents SQL injection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

log = logging.getLogger("nexora.db")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_db_time(dt: datetime) -> str:
    """Store timestamps as naive UTC 'YYYY-MM-DD HH:MM:SS' (matches SQLite datetime('now'))."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def from_db_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def jload(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.migrate()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    # ------------------------------------------------------------ migrations
    async def migrate(self) -> list[str]:
        assert self.conn
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
        )
        cur = await self.conn.execute("SELECT name FROM schema_migrations")
        done = {r[0] for r in await cur.fetchall()}
        applied = []
        for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if file.name in done:
                continue
            log.info("Applying migration %s", file.name)
            await self.conn.executescript(file.read_text(encoding="utf-8"))
            await self.conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (file.name,))
            applied.append(file.name)
        await self.conn.commit()
        return applied

    # --------------------------------------------------------------- helpers
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement and return lastrowid."""
        assert self.conn
        async with self._write_lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cur.lastrowid or 0

    async def execute_rowcount(self, sql: str, params: Iterable[Any] = ()) -> int:
        assert self.conn
        async with self._write_lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cur.rowcount

    async def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        assert self.conn
        async with self._write_lock:
            await self.conn.executemany(sql, [tuple(p) for p in seq])
            await self.conn.commit()

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        assert self.conn
        cur = await self.conn.execute(sql, tuple(params))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        assert self.conn
        cur = await self.conn.execute(sql, tuple(params))
        return [dict(r) for r in await cur.fetchall()]

    async def fetchval(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        assert self.conn
        cur = await self.conn.execute(sql, tuple(params))
        row = await cur.fetchone()
        return row[0] if row and row[0] is not None else default

    async def next_number(self, table: str, column: str, guild_id: int) -> int:
        """Per-guild incrementing counter (case numbers, ticket numbers...).

        `table`/`column` are always internal constants, never user input.
        """
        assert table.isidentifier() and column.isidentifier()
        val = await self.fetchval(f"SELECT MAX({column}) FROM {table} WHERE guild_id = ?", (guild_id,), 0)
        return int(val) + 1

    # ---------------------------------------------------------------- backup
    async def backup(self, dest: Path) -> Path:
        assert self.conn
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _copy():
            src = sqlite3.connect(self.path)
            dst = sqlite3.connect(str(dest))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()

        async with self._write_lock:
            await self.conn.commit()
            await asyncio.to_thread(_copy)
        return dest

    async def restore(self, source: Path) -> None:
        """Replace the live database contents with a backup file."""
        assert self.conn

        def _copy():
            src = sqlite3.connect(str(source))
            check = src.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                src.close()
                raise ValueError(f"Backup failed integrity check: {check}")
            dst = sqlite3.connect(self.path)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()

        async with self._write_lock:
            await self.conn.close()
            try:
                await asyncio.to_thread(_copy)
            finally:
                self.conn = await aiosqlite.connect(self.path)
                self.conn.row_factory = aiosqlite.Row
                await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.migrate()

    async def log_error(self, source: str, message: str, tb: str | None = None, guild_id: int | None = None) -> None:
        try:
            await self.execute(
                "INSERT INTO error_log (source, guild_id, message, traceback) VALUES (?, ?, ?, ?)",
                (source, guild_id, message[:2000], (tb or "")[:8000]),
            )
        except Exception:  # never let error logging crash the caller
            log.exception("Failed to persist error")
