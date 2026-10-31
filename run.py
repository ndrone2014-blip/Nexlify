"""Start Nexora: the Discord bot and the website run together in one process,
sharing one database connection and one settings cache."""
from __future__ import annotations

import asyncio
import logging
import sys

import discord
import uvicorn

from nexora.bot import EXTENSIONS, NexoraBot
from nexora.config import config
from nexora.db import Database
from nexora.web.app import create_app

log = logging.getLogger("nexora")


async def load_extensions_offline(bot: NexoraBot) -> None:
    """Load cogs without logging in, so the website's command list and APIs still work."""
    for ext in EXTENSIONS:
        if ext not in bot.extensions:
            await bot.load_extension(ext)


async def run_bot(bot: NexoraBot) -> None:
    try:
        await bot.start(config.bot_token)
    except discord.LoginFailure:
        log.error("Discord rejected DISCORD_BOT_TOKEN. Reset the token in the Developer Portal and update .env. The website keeps running.")
    except discord.PrivilegedIntentsRequired:
        log.error("Enable 'Server Members Intent' and 'Message Content Intent' in the Developer Portal (Bot tab), then restart.")
    except Exception:
        log.exception("The bot stopped unexpectedly. The website keeps running.")
    await load_extensions_offline(bot)


async def main() -> None:
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    db = Database(config.database_path)
    await db.connect()
    bot = NexoraBot(config, db)
    app = create_app(config, db, bot)
    server = uvicorn.Server(uvicorn.Config(app, host=config.web_host, port=config.web_port, log_level="warning", proxy_headers=True))

    async with bot:
        if config.bot_token:
            bot_task = asyncio.create_task(run_bot(bot))
        else:
            log.warning("DISCORD_BOT_TOKEN is empty, so only the website is running. Fill in .env and restart.")
            await load_extensions_offline(bot)
            bot_task = None
        if not config.oauth_enabled:
            log.warning("DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET are empty, so dashboard login is disabled.")
        log.info("Website: http://%s:%s  (public URL: %s)", "localhost" if config.web_host in {"127.0.0.1", "0.0.0.0"} else config.web_host,
                 config.web_port, config.public_url)
        try:
            await server.serve()
        finally:
            if bot_task:
                bot_task.cancel()
    await db.close()


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        sys.exit("Nexora needs Python 3.11 or newer (3.12+ recommended).")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
