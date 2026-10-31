"""Build the public Nexora website as static files for Netlify.

Netlify can host static pages but can't run the Discord bot or the dashboard
(they need a process that stays online). This script renders the public pages
(home, features, commands, docs, support) into ./site and writes Netlify
redirect rules that send /dashboard, login, invite, status and leaderboards to
wherever your bot is running.

Environment variables (set them in Netlify: Site configuration > Environment variables):
  DASHBOARD_URL      Public address of your running bot + dashboard, e.g. https://nexora.example.com
  DISCORD_CLIENT_ID  Optional. Makes "Add to Discord" go straight to Discord's invite page.
  SUPPORT_URL        Optional. Link shown on the Support page.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "site"

PAGES = {"/": "home.html", "/features": "features.html", "/commands": "commands.html", "/docs": "docs.html", "/support": "support.html"}


async def command_groups() -> list[dict]:
    """Load the bot's modules offline (no token, no network) to list the real slash commands."""
    os.environ.setdefault("DATABASE_PATH", str(Path(tempfile.mkdtemp()) / "build.db"))
    from nexora.bot import EXTENSIONS, NexoraBot
    from nexora.config import Config
    from nexora.db import Database
    from nexora.web.app import command_catalog

    cfg = Config(bot_token="", database_path=Path(os.environ["DATABASE_PATH"]), backup_dir=Path(tempfile.mkdtemp()))
    db = Database(cfg.database_path)
    await db.connect()
    bot = NexoraBot(cfg, db)
    async with bot:
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        groups = command_catalog(bot)
    await db.close()
    return groups


def main() -> None:
    from nexora.web.app import FEATURES, templates

    dashboard = os.getenv("DASHBOARD_URL", "").strip().rstrip("/")
    client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    invite = (f"https://discord.com/oauth2/authorize?client_id={client_id}&permissions=8&scope=bot%20applications.commands"
              if client_id else (f"{dashboard}/invite" if dashboard else ""))
    config = SimpleNamespace(support_url=os.getenv("SUPPORT_URL", ""), owner_ids=[])
    groups = asyncio.run(command_groups())

    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(ROOT / "nexora" / "web" / "static", OUT / "static")

    for path, template in PAGES.items():
        request = SimpleNamespace(url=SimpleNamespace(path=path))
        html = templates.get_template(template).render(
            request=request, config=config, user=None, invite_url=invite, oauth=bool(dashboard),
            features=FEATURES if path == "/features" else FEATURES[:6], servers=None, groups=groups)
        target = OUT / "index.html" if path == "/" else OUT / path.strip("/") / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")

    error = templates.get_template("error.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/404")), config=config, user=None, invite_url=invite,
        code=404, message="That page doesn't exist.")
    (OUT / "404.html").write_text(error, encoding="utf-8")

    # Pages that need the running bot are forwarded to it.
    target = dashboard or "/not-connected"
    lines = []
    if invite:
        lines.append(f"/invite {invite} 302")
    for src in ("/dashboard", "/dashboard/*", "/auth/*", "/status", "/system", "/leaderboard/*", "/api/*"):
        dest = f"{dashboard}{src.replace('*', ':splat')}" if dashboard else "/not-connected/"
        lines.append(f"{src} {dest} 302")
    if not invite:
        lines.append(f"/invite {'/not-connected/' if not dashboard else target + '/invite'} 302")
    (OUT / "_redirects").write_text("\n".join(lines) + "\n", encoding="utf-8")

    nc = templates.get_template("error.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/not-connected")), config=config, user=None, invite_url=invite, code=503,
        message="The dashboard isn't connected yet. Set DASHBOARD_URL in Netlify's environment variables to the public address of your running Nexora bot, then redeploy.")
    (OUT / "not-connected").mkdir(exist_ok=True)
    (OUT / "not-connected" / "index.html").write_text(nc, encoding="utf-8")

    print(f"Built {len(PAGES)} pages into {OUT}")
    print(f"Dashboard links point to: {dashboard or '(not set: showing a setup message)'}")


if __name__ == "__main__":
    main()
