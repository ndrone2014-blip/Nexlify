# Nexora

An all-in-one Discord bot with a full web dashboard: advanced logging, moderation, automod, tickets, applications, giveaways, leveling, economy, automations and more. The bot and the website run together on your own PC and share one database, so anything you change on the dashboard takes effect in Discord immediately.

## Quick start (Windows)

1. Install **Python 3.12+** from [python.org](https://www.python.org/downloads/). Tick *Add python.exe to PATH*.
2. Double-click **`install.bat`**. It creates a virtual environment, installs everything and creates `.env`.
3. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications):
   - **Bot** tab: reset the token and paste it into `DISCORD_BOT_TOKEN`. Turn on **Server Members Intent** and **Message Content Intent**.
   - **OAuth2** tab: copy the Client ID and Client Secret into `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET`, and add the redirect `http://localhost:8000/auth/callback`.
   - Put your own Discord user ID in `BOT_OWNER_IDS` (right-click yourself in Discord with Developer Mode on, then *Copy User ID*).
   - Optional: set `DEV_GUILD_ID` to your test server so slash commands appear instantly.
4. Double-click **`start.bat`**, open <http://localhost:8000>, click **Add to Discord**, then **Dashboard**.

Nothing paid is required. The `/ask` AI command is optional and only turns on if you add your own `ANTHROPIC_API_KEY`.

**Updating:** run `update.bat` (needs Git). Your database is backed up first and migrations run automatically.
**VS Code:** open the folder; *Run Nexora* and *Run tests* launch configurations are included.

## What's included

| Area | Discord | Dashboard |
|---|---|---|
| Advanced logging | Messages (edit before/after, delete, bulk delete with file), members (join/leave/nick/username/avatar/boost/screening), roles (create/delete/permission & position diffs/assignments), channels & threads (incl. permission overwrites), moderation, voice (join/leave/move/mute/deafen/stream/stage), server (settings, invites, webhooks, integrations, emojis). Per-category toggles & channels, ignored channels/roles. | Live feed, search, filters (category, event, user, moderator, dates), event details with before/after, CSV/JSON export, retention setting |
| Moderation | `/ban` (temp bans) `/unban` `/kick` `/timeout` `/untimeout` `/warn` `/warnings` `/clearwarnings` `/delwarn` `/purge` `/slowmode` `/lock` `/unlock` `/nickname` `/case` `/history` `/note` `/modstats` `/appeal` | Cases, warnings, appeals (accept reverses the punishment), user lookup with notes and actions, escalation rules |
| Automod | Spam/flood, repeated messages, invites, links (allowlist), scam links, profanity, mention spam, emoji spam, caps, attachments, anti-raid (alert/timeout/kick/lockdown, `/raid end`), suspicious accounts. Every rule can delete, warn, time out, or only flag. | Rule configuration, review queue with one-click false-positive undo |
| Tickets | Panels with buttons, up to 5 questions, private channels, claim, transfer, add/remove, priority, rename, close (with reason), reopen, HTML transcripts, ratings, auto-close | Panel builder, publish, ticket list & stats, transcripts, close from the web |
| Applications | Apply button or `/apply`, unlimited questions (short/long/multiple choice, run in steps), cooldowns, limits, review channel with accept/deny/note buttons, multi-reviewer votes, auto roles, DMs | Form builder, review dashboard with votes, notes and history, stats |
| Giveaways | `/giveaway start/end/reroll/cancel/extend/list`, button entry, required & bonus roles, account age, minimum level, scheduling, verified winners | Create, manage, history and stats |
| Community | Welcome/goodbye (embeds, banners, button, DM), auto roles, button/dropdown/reaction role menus, temp roles, leveling with rewards & multipliers, economy (daily streaks, weekly, work, pay, shop, inventory, achievements), suggestions, polls, starboard, counting, birthdays, reminders, member-count channels | Settings for each, shop editor, leaderboards (also public at `/leaderboard/<server id>`) |
| Tools | Custom commands (prefix), `/announce`, `/embed`, scheduled announcements, `/config export/import` | Embed builder with live preview, custom commands, scheduled messages, automation builder (15 triggers, 11 conditions, 11 actions, run history), analytics with charts and CSV export, config backup/import, audit log |
| Platform | | Discord OAuth2, server-side sessions, CSRF, rate limiting, security headers, permission checks on every action, read-only roles, status page, commands page generated from the bot, System page for owners (errors, performance, feature usage, database backup/restore) |

## Honest limits (set by Discord, not Nexora)

- Content of edited or deleted messages is only known for messages Nexora saw while running (it caches the last 5,000).
- The audit log doesn't always say who did something (e.g. members deleting their own messages). Logs then say so instead of guessing.
- Deleted attachments are removed by Discord, so only file names and sizes are logged.
- Modals hold at most 5 text inputs, so long application forms run over several steps.
- Channel renames (priority, stats channels) are rate limited by Discord to 2 per 10 minutes.

## Project layout

```
run.py                  starts the bot and website together
nexora/config.py        settings from .env
nexora/db/              async SQLite layer + numbered SQL migrations
nexora/core/            settings schema (drives both bot and dashboard), pure logic
nexora/bot/cogs/        one module per feature
nexora/web/             FastAPI app, API routers, templates, static JS/CSS
tests/                  68 automated tests
```

The settings schema in `nexora/core/schema.py` is the single source of truth: adding a field there adds it to the dashboard, validation and the bot defaults.

## Development checklist

| Stage | Status |
|---|---|
| Architecture, database, migrations, settings schema | ✅ Built and tested |
| Logging, moderation, automod | ✅ Built and tested |
| Tickets, applications, giveaways | ✅ Built and tested |
| Welcome, roles, leveling, economy, suggestions, polls, starboard, counting, birthdays, reminders, stat channels | ✅ Built; core paths tested |
| Custom commands, embeds, scheduled messages, automation engine, AI assistant | ✅ Built; automation and commands tested |
| Website, OAuth, dashboard for every module, analytics, backups | ✅ Built; API tested end to end, every page loaded in a real browser with 0 JS errors |
| Live test against Discord with a real token | ⏳ Needs your token (see Quick start) |

Run the tests with `.venv\Scripts\python -m pip install -r requirements-dev.txt` then `.venv\Scripts\python -m pytest`.

## Moving to PostgreSQL later

All queries go through `nexora/db` with parameter binding and the schema is plain SQL, so a port means swapping aiosqlite for asyncpg in one file and converting `AUTOINCREMENT` columns.
