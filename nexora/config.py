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

    # Misc
    brand_color: int = 0x7C3AED
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        if not self.session_secret:
            # Ephemeral secret: sessions reset on restart. install.bat writes a permanent one.
            self.session_secret = secrets.token_urlsafe(48)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    @property
    def oauth_enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def invite_url(self) -> str:
        if not self.client_id:
            return ""
        # Administrator keeps setup simple for server owners; they can narrow it in Discord.
        return (
            f"https://discord.com/oauth2/authorize?client_id={self.client_id}"
            "&permissions=8&scope=bot%20applications.commands"
        )


config = Config()
