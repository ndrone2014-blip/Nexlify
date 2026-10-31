"""Cached access to per-guild module settings."""
from __future__ import annotations

from nexora.core import schema
from nexora.db import Database, jdump, jload


class SettingsService:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[tuple[int, str], dict] = {}

    async def get(self, guild_id: int, module: str) -> dict:
        key = (guild_id, module)
        if key in self._cache:
            return self._cache[key]
        row = await self.db.fetchone(
            "SELECT data FROM guild_settings WHERE guild_id = ? AND module = ?", (guild_id, module)
        )
        merged = schema.defaults(module)
        stored = jload(row["data"], {}) if row else {}
        # Only keep keys that still exist in the schema (old keys are ignored after upgrades).
        merged.update({k: v for k, v in stored.items() if k in merged})
        self._cache[key] = merged
        return merged

    async def update(self, guild_id: int, module: str, changes: dict) -> dict:
        clean = schema.validate(module, changes, partial=True)
        current = dict(await self.get(guild_id, module))
        current.update(clean)
        await self.db.execute(
            """INSERT INTO guild_settings (guild_id, module, data, updated_at) VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(guild_id, module) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at""",
            (guild_id, module, jdump(current)),
        )
        self._cache[(guild_id, module)] = current
        return current

    async def reset(self, guild_id: int, module: str) -> dict:
        await self.db.execute("DELETE FROM guild_settings WHERE guild_id = ? AND module = ?", (guild_id, module))
        self._cache.pop((guild_id, module), None)
        return await self.get(guild_id, module)

    async def export_guild(self, guild_id: int) -> dict:
        return {m: await self.get(guild_id, m) for m in schema.MODULES}

    async def import_guild(self, guild_id: int, data: dict) -> list[str]:
        """Restore a configuration backup. Returns modules that were imported."""
        imported = []
        for module, values in data.items():
            if module in schema.MODULES and isinstance(values, dict):
                await self.update(guild_id, module, values)
                imported.append(module)
        return imported

    def invalidate(self, guild_id: int | None = None) -> None:
        if guild_id is None:
            self._cache.clear()
        else:
            for k in [k for k in self._cache if k[0] == guild_id]:
                self._cache.pop(k, None)
