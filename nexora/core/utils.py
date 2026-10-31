"""Shared helpers: placeholders, durations, branded embeds."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import discord

BRAND = 0x7C3AED
SUCCESS = 0x22C55E
WARNING = 0xF59E0B
DANGER = 0xEF4444
INFO = 0x6366F1

_DURATION_RE = re.compile(r"(\d+)\s*(w|d|h|m|s)", re.I)
_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str | None) -> timedelta | None:
    """Parse '1d2h', '30m', '45s', '2w'. Returns None when nothing parses."""
    if not text:
        return None
    text = text.strip().lower()
    if text.isdigit():  # bare number = minutes
        return timedelta(minutes=int(text))
    total = sum(int(n) * _UNITS[u.lower()] for n, u in _DURATION_RE.findall(text))
    return timedelta(seconds=total) if total > 0 else None


def human_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "permanent"
    seconds = int(seconds)
    parts = []
    for name, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{name}")
            seconds %= size
    return " ".join(parts[:3]) or "0s"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def render(template: str, member: discord.abc.User | None = None, guild: discord.Guild | None = None, **extra: Any) -> str:
    """Fill {placeholders}. Unknown placeholders are left untouched."""
    values: dict[str, Any] = {}
    if member is not None:
        values.update(
            {
                "user": member.mention,
                "user.mention": member.mention,
                "user.name": member.name,
                "user.display_name": getattr(member, "display_name", member.name),
                "user.id": member.id,
                "user.avatar": member.display_avatar.url,
                "user.created": discord.utils.format_dt(member.created_at, "D"),
            }
        )
    if guild is not None:
        count = guild.member_count or len(guild.members)
        values.update(
            {
                "server": guild.name,
                "server.name": guild.name,
                "server.id": guild.id,
                "member_count": count,
                "member_count_ordinal": ordinal(count),
            }
        )
    values.update({k: v for k, v in extra.items()})
    return re.sub(r"\{([\w.]+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def color_int(value: str | int | None, default: int = BRAND) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).lstrip("#"), 16)
    except ValueError:
        return default


def brand_embed(title: str | None = None, description: str | None = None, color: int = BRAND, **kwargs) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, **kwargs)
    embed.set_footer(text="Nexora")
    return embed


def ok(description: str, title: str | None = None) -> discord.Embed:
    return brand_embed(title, f"✅ {description}", SUCCESS)


def err(description: str, title: str | None = None) -> discord.Embed:
    return brand_embed(title, f"❌ {description}", DANGER)


def truncate(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def embed_has_content(data: dict | None) -> bool:
    if not data:
        return False
    return any([data.get("title"), data.get("description"), data.get("fields"), data.get("image"), data.get("thumbnail"),
                (data.get("author") or {}).get("name"), (data.get("footer") or {}).get("text")])


def embed_from_dict(data: dict, member=None, guild=None) -> discord.Embed:
    """Build an embed from the dashboard embed-builder JSON format (validated elsewhere)."""

    def r(s):
        return render(s, member, guild) if s else s

    embed = discord.Embed(
        title=truncate(r(data.get("title")), 256) or None,
        description=truncate(r(data.get("description")), 4096) or None,
        url=data.get("url") or None,
        color=color_int(data.get("color")),
    )
    author = data.get("author") or {}
    if author.get("name"):
        embed.set_author(name=truncate(r(author["name"]), 256), url=author.get("url") or None, icon_url=author.get("icon_url") or None)
    footer = data.get("footer") or {}
    if footer.get("text"):
        embed.set_footer(text=truncate(r(footer["text"]), 2048), icon_url=footer.get("icon_url") or None)
    if data.get("image"):
        embed.set_image(url=data["image"])
    if data.get("thumbnail"):
        embed.set_thumbnail(url=data["thumbnail"])
    for f in (data.get("fields") or [])[:25]:
        if f.get("name") and f.get("value"):
            embed.add_field(name=truncate(r(f["name"]), 256), value=truncate(r(f["value"]), 1024), inline=bool(f.get("inline")))
    if data.get("timestamp"):
        embed.timestamp = discord.utils.utcnow()
    return embed


def link_buttons_view(buttons: list[dict] | None) -> discord.ui.View | None:
    buttons = [b for b in (buttons or []) if b.get("label") and str(b.get("url", "")).startswith(("http://", "https://"))]
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for b in buttons[:5]:
        view.add_item(discord.ui.Button(label=truncate(b["label"], 80), url=b["url"]))
    return view
