"""Central configuration loaded from environment variables / .env file."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is in requirements.txt
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent
if load_dotenv:
    load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _int_list(name: str) -> list[int]:
    raw = os.getenv(name, "")
    return [int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()]


# Discord permission bits Nexora needs (https://discord.com/developers/docs/topics/permissions).
_INVITE_BITS = {
    "kick_members": 1, "ban_members": 2, "manage_channels": 4, "manage_guild": 5, "add_reactions": 6, "view_audit_log": 7,
    "view_channel": 10, "send_messages": 11, "manage_messages": 13, "embed_links": 14, "attach_files": 15,
    "read_message_history": 16, "use_external_emojis": 18, "manage_nicknames": 27, "manage_roles": 28,
    "create_public_threads": 35, "send_messages_in_threads": 38, "moderate_members": 40,
}
INVITE_PERMISSIONS = sum(1 << bit for bit in _INVITE_BITS.values())


@dataclass
class Config:
    # Discord
    bot_token: str = field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    client_id: str = field(default_factory=lambda: os.getenv("DISCORD_CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: os.getenv("DISCORD_CLIENT_SECRET", ""))
    redirect_uri: str = field(
        default_factory=lambda: os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8000/auth/callback")
    )
    owner_ids: list[int] = field(default_factory=lambda: _int_list("BOT_OWNER_IDS"))
    dev_guild_id: int | None = field(
        default_factory=lambda: int(os.getenv("DEV_GUILD_ID")) if os.getenv("DEV_GUILD_ID", "").isdigit() else None
    )

    # Web
    web_host: str = field(default_factory=lambda: os.getenv("WEB_HOST", "127.0.0.1"))
    web_port: int = field(default_factory=lambda: int(os.getenv("WEB_PORT", "8000")))
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/"))
    session_secret: str = field(default_factory=lambda: os.getenv("SESSION_SECRET", ""))
    secure_cookies: bool = field(default_factory=lambda: _bool("SECURE_COOKIES", False))
    support_url: str = field(default_factory=lambda: os.getenv("SUPPORT_URL", ""))

    # Storage
    database_path: Path = field(
        default_factory=lambda: Path(os.getenv("DATABASE_PATH", str(ROOT / "data" / "nexora.db")))
    )
    backup_dir: Path = field(default_factory=lambda: Path(os.getenv("BACKUP_DIR", str(ROOT / "data" / "backups"))))

    # Optional AI assistant (disabled unless a key is provided)
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    ai_model: str = field(default_factory=lambda: os.getenv("AI_MODEL", "claude-haiku-5-5"))
    # Spending guards: /ask uses YOUR key, so cap how much any server (and all servers together) can use per day.
    ai_daily_limit: int = field(default_factory=lambda: int(os.getenv("AI_DAILY_LIMIT", "300") or 0))
    ai_guild_daily_limit: int = field(default_factory=lambda: int(os.getenv("AI_GUILD_DAILY_LIMIT", "30") or 0))
    # Optional: only these server IDs may use /ask (empty = any server that turns it on).
    ai_allowed_guild_ids: list[int] = field(default_factory=lambda: _int_list("AI_ALLOWED_GUILDS"))

    # Misc
    brand_color: int = 0x7C3AED
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        if self.public_url.startswith("https://"):
            # Cookies must never travel over plain HTTP once the dashboard is public.
            self.secure_cookies = True
        if not self.session_secret:
            # Ephemeral secret: sessions reset on restart. install.bat writes a permanent one.
            self.session_secret = secrets.token_urlsafe(48)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def security_warnings(self) -> list[str]:
        """Setup mistakes that would weaken security on a public bot."""
        out = []
        if not os.getenv("SESSION_SECRET"):
            out.append("SESSION_SECRET is empty: everyone is logged out on every restart. Run install.bat or set a long random value.")
        elif len(self.session_secret) < 32:
            out.append("SESSION_SECRET is short. Use at least 32 random characters.")
        if self.web_host not in {"127.0.0.1", "localhost", "::1"}:
            out.append(f"WEB_HOST is {self.web_host}: the dashboard is reachable directly, bypassing your tunnel/HTTPS. "
                       "Keep WEB_HOST=127.0.0.1 and expose it through a tunnel or reverse proxy.")
        if not self.public_url.startswith("https://") and "localhost" not in self.public_url and "127.0.0.1" not in self.public_url:
            out.append("PUBLIC_URL isn't HTTPS. Logins and cookies can be stolen over plain HTTP; use an https:// address.")
        if self.redirect_uri.startswith("http://") and "localhost" not in self.redirect_uri and "127.0.0.1" not in self.redirect_uri:
            out.append("DISCORD_REDIRECT_URI isn't HTTPS. Use your https:// address.")
        if not self.owner_ids:
            out.append("BOT_OWNER_IDS is empty, so nobody can open the System page (backups, errors).")
        return out

    @property
    def oauth_enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def invite_url(self) -> str:
        if not self.client_id:
            return ""
        # Only the permissions Nexora actually uses. Never Administrator: if a bug were ever found, an admin bot
        # hands the attacker every server it's in. Mention Everyone is left out on purpose too, so Discord itself
        # stops @everyone pings unless a server owner chooses to allow them.
        return (
            f"https://discord.com/oauth2/authorize?client_id={self.client_id}"
            f"&permissions={INVITE_PERMISSIONS}&scope=bot%20applications.commands"
        )


config = Config()
