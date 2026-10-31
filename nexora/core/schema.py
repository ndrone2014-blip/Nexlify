"""Declarative per-guild settings schema.

Every configurable module is described once here. The bot reads settings through
`SettingsService` (defaults merged in), and the dashboard renders its forms from
the same schema and validates submissions against it, so a setting can never
exist in the UI without existing in the backend (or vice versa).
"""
from __future__ import annotations

from typing import Any

PUNISH_ACTIONS = [
    ("delete", "Delete message"),
    ("delete_warn", "Delete + warn"),
    ("delete_timeout", "Delete + timeout"),
    ("warn", "Warn only"),
    ("timeout", "Timeout only"),
    ("flag", "Flag for moderator review (no action)"),
]


def F(key: str, type: str, label: str, default: Any = None, **extra) -> dict:
    return {"key": key, "type": type, "label": label, "default": default, **extra}


def section(title: str, *fields: dict) -> dict:
    return {"title": title, "fields": list(fields)}


LOG_CATEGORIES = {
    "messages": "Message edits, deletions and bulk deletions",
    "members": "Joins, leaves, nickname/username/avatar changes, boosts, screening",
    "roles": "Role creation, deletion, permission & position changes, role assignments",
    "channels": "Channel/thread creation, deletion, renames, topics, permission overwrites",
    "moderation": "Bans, unbans, kicks, timeouts, warnings, purges",
    "voice": "Voice joins/leaves/moves, server mute/deafen, streaming, stage",
    "server": "Server settings, icon/name, invites, webhooks, integrations, emojis",
}


def _automod_rule(key: str, title: str, *fields: dict, default_action: str = "delete_warn") -> dict:
    return section(
        title,
        F(f"{key}_enabled", "bool", "Enabled", False),
        *fields,
        F(f"{key}_action", "select", "Action", default_action, options=PUNISH_ACTIONS),
    )


MODULES: dict[str, dict] = {
    "general": {
        "title": "Server Settings",
        "icon": "settings",
        "description": "Core behaviour, staff roles and dashboard access.",
        "sections": [
            section(
                "Basics",
                F("prefix", "text", "Prefix for custom commands", "!", max_length=5),
                F("embed_color", "color", "Embed accent color", "#7C3AED"),
                F("staff_role_ids", "roles", "Staff roles", [], help="Used for staff activity tracking and ticket/application defaults."),
            ),
            section(
                "Dashboard access",
                F(
                    "dashboard_role_ids",
                    "roles",
                    "Extra roles allowed to use the dashboard",
                    [],
                    help="Members with Manage Server always have access. Roles here get access too.",
                ),
                F(
                    "dashboard_readonly_role_ids",
                    "roles",
                    "Read-only dashboard roles",
                    [],
                    help="Can view the dashboard and logs but cannot change anything.",
                ),
            ),
        ],
    },
    "logging": {
        "title": "Advanced Logging",
        "icon": "scroll",
        "description": "Choose which events are recorded and where they are posted.",
        "sections": [
            section(
                "General",
                F("enabled", "bool", "Logging enabled", True),
                F("default_channel_id", "channel", "Default log channel", None, help="Used by any category without its own channel."),
                F("retention_days", "int", "Keep logs in the database for (days)", 90, min=1, max=3650),
                F("ignore_bots", "bool", "Ignore bot messages", True),
                F("ignored_channel_ids", "channels", "Ignored channels", []),
                F("ignored_role_ids", "roles", "Ignored roles", []),
            ),
            *[
                section(
                    f"{name.title()} logs",
                    F(f"{name}_enabled", "bool", "Enabled", True, help=desc),
                    F(f"{name}_channel_id", "channel", "Channel (optional override)", None),
                )
                for name, desc in LOG_CATEGORIES.items()
            ],
        ],
    },
    "moderation": {
        "title": "Moderation",
        "icon": "shield",
        "description": "Punishment behaviour, warning escalation and appeals.",
        "sections": [
            section(
                "Behaviour",
                F("dm_users", "bool", "DM users when they are punished", True),
                F("require_reason", "bool", "Require a reason for every action", False),
                F("show_moderator", "bool", "Show the moderator's name in DMs", False),
                F("default_timeout_minutes", "int", "Default timeout length (minutes)", 60, min=1, max=40320),
                F("warn_expiry_days", "int", "Warnings expire after (days, 0 = never)", 0, min=0, max=3650),
            ),
            section(
                "Warning escalation",
                F("escalation_enabled", "bool", "Automatically punish on warning thresholds", True),
                F(
                    "escalation",
                    "rows",
                    "Escalation rules",
                    [
                        {"warnings": 3, "action": "timeout", "duration_minutes": 60},
                        {"warnings": 5, "action": "kick", "duration_minutes": 0},
                        {"warnings": 7, "action": "ban", "duration_minutes": 0},
                    ],
                    columns=[
                        F("warnings", "int", "Active warnings", 3, min=1, max=100),
                        F("action", "select", "Action", "timeout", options=[("timeout", "Timeout"), ("kick", "Kick"), ("ban", "Ban")]),
                        F("duration_minutes", "int", "Duration (minutes, timeout/ban; 0 = permanent ban)", 60, min=0, max=525600),
                    ],
                    max_rows=20,
                ),
            ),
            section(
                "Appeals",
                F("appeals_enabled", "bool", "Allow users to appeal with /appeal", True),
                F("appeals_channel_id", "channel", "Appeal review channel", None),
            ),
        ],
    },
    "automod": {
        "title": "Automod",
        "icon": "bot",
        "description": "Automatic protection. Every rule can delete, warn, time out, or just flag for review.",
        "sections": [
            section(
                "General",
                F("enabled", "bool", "Automod enabled", False),
                F("log_channel_id", "channel", "Automod alert channel", None),
                F("exempt_role_ids", "roles", "Exempt roles", []),
                F("exempt_channel_ids", "channels", "Exempt channels", []),
                F("timeout_minutes", "int", "Timeout length for timeout actions (minutes)", 10, min=1, max=40320),
                F("exempt_staff", "bool", "Exempt members with Manage Messages", True),
            ),
            _automod_rule(
                "spam", "Anti-spam / anti-flood",
                F("spam_max_messages", "int", "Max messages", 6, min=2, max=50),
                F("spam_seconds", "int", "Within seconds", 5, min=1, max=120),
                default_action="delete_timeout",
            ),
            _automod_rule(
                "repeat", "Repeated messages",
                F("repeat_count", "int", "Identical messages allowed in a row", 3, min=2, max=20),
            ),
            _automod_rule(
                "invites", "Anti-invite",
                F("invites_allow_own", "bool", "Allow invites to this server", True),
            ),
            _automod_rule(
                "links", "Anti-link",
                F("links_allowed_domains", "string_list", "Allowed domains", ["youtube.com", "youtu.be", "tenor.com", "giphy.com"]),
                default_action="delete",
            ),
            _automod_rule(
                "malicious", "Malicious link filter",
                F("malicious_domains", "string_list", "Extra blocked domains", [], help="A built-in list of common scam/phishing patterns is always checked."),
                default_action="delete_timeout",
            ),
            _automod_rule(
                "profanity", "Profanity filter",
                F("profanity_words", "string_list", "Blocked words", []),
                F("profanity_wildcard", "bool", "Match inside words", False),
                default_action="delete",
            ),
            _automod_rule(
                "mentions", "Mention spam",
                F("mentions_max", "int", "Max mentions per message", 5, min=1, max=50),
                default_action="delete_timeout",
            ),
            _automod_rule(
                "emoji", "Emoji spam",
                F("emoji_max", "int", "Max emojis per message", 10, min=1, max=100),
                default_action="delete",
            ),
            _automod_rule(
                "caps", "Excessive caps",
                F("caps_percent", "int", "Max % capital letters", 70, min=10, max=100),
                F("caps_min_length", "int", "Only check messages longer than", 12, min=1, max=500),
                default_action="delete",
            ),
            _automod_rule(
                "attachments", "Attachment filter",
                F("attachments_blocked_ext", "string_list", "Blocked file extensions", ["exe", "scr", "bat", "cmd", "msi", "vbs", "jar"]),
                default_action="delete_warn",
            ),
            section(
                "Anti-raid",
                F("raid_enabled", "bool", "Enabled", False),
                F("raid_joins", "int", "Joins", 10, min=3, max=200),
                F("raid_seconds", "int", "Within seconds", 10, min=2, max=600),
                F(
                    "raid_action",
                    "select",
                    "Action during a raid",
                    "alert",
                    options=[("alert", "Alert staff only"), ("timeout", "Timeout new joiners"), ("kick", "Kick new joiners"), ("lockdown", "Lock all text channels + alert")],
                ),
            ),
            section(
                "Suspicious accounts",
                F("newaccount_enabled", "bool", "Enabled", False),
                F("newaccount_min_age_days", "int", "Minimum account age (days)", 3, min=1, max=365),
                F(
                    "newaccount_action",
                    "select",
                    "Action",
                    "flag",
                    options=[("flag", "Flag for review"), ("timeout", "Timeout"), ("kick", "Kick")],
                ),
                F("newaccount_no_avatar", "bool", "Also flag accounts with no avatar", False),
            ),
        ],
    },
    "tickets": {
        "title": "Tickets",
        "icon": "ticket",
        "description": "Global ticket behaviour. Panels are managed on the Tickets page.",
        "sections": [
            section(
                "General",
                F("enabled", "bool", "Tickets enabled", True),
                F("transcript_channel_id", "channel", "Transcript channel", None),
                F("dm_transcript", "bool", "DM transcript to the ticket opener", True),
                F("ratings_enabled", "bool", "Ask for a rating after closing", True),
                F("ping_staff", "bool", "Ping staff roles when a ticket opens", True),
                F("delete_after_close_seconds", "int", "Delete channel after closing (seconds)", 10, min=0, max=86400),
                F("auto_close_hours", "int", "Auto-close inactive tickets after (hours, 0 = never)", 0, min=0, max=720),
            ),
        ],
    },
    "applications": {
        "title": "Applications",
        "icon": "clipboard",
        "description": "Global application behaviour. Forms are built on the Applications page.",
        "sections": [
            section(
                "General",
                F("enabled", "bool", "Applications enabled", True),
                F("default_review_channel_id", "channel", "Default review channel", None),
                F("dm_on_submit", "bool", "DM applicants a confirmation", True),
                F("dm_on_decision", "bool", "DM applicants the decision", True),
            ),
        ],
    },
    "giveaways": {
        "title": "Giveaways",
        "icon": "gift",
        "description": "Defaults for new giveaways.",
        "sections": [
            section(
                "General",
                F("manager_role_ids", "roles", "Giveaway manager roles", [], help="Besides Manage Server."),
                F("dm_winners", "bool", "DM winners", True),
                F("ping_role_id", "role", "Role to ping on new giveaways", None),
                F("button_label", "text", "Entry button label", "Enter", max_length=40),
            ),
        ],
    },
    "welcome": {
        "title": "Welcome & Goodbye",
        "icon": "wave",
        "description": "Greet and say goodbye to members. Placeholders: {user} {user.name} {user.id} {server} {member_count} {member_count_ordinal}",
        "sections": [
            section(
                "Welcome",
                F("welcome_enabled", "bool", "Enabled", False),
                F("welcome_channel_id", "channel", "Channel", None),
                F("welcome_message", "textarea", "Message", "Welcome {user} to **{server}**! You are our {member_count_ordinal} member.", max_length=2000),
                F("welcome_embed", "bool", "Send as embed", True),
                F("welcome_title", "text", "Embed title", "Welcome!", max_length=256),
                F("welcome_image_url", "url", "Banner image URL", ""),
                F("welcome_thumbnail_avatar", "bool", "Show member avatar", True),
                F("welcome_button_label", "text", "Link button label (optional)", "", max_length=80),
                F("welcome_button_url", "url", "Link button URL", ""),
            ),
            section(
                "Direct message",
                F("dm_enabled", "bool", "DM new members", False),
                F("dm_message", "textarea", "DM message", "Welcome to {server}, {user.name}!", max_length=2000),
            ),
            section(
                "Goodbye",
                F("goodbye_enabled", "bool", "Enabled", False),
                F("goodbye_channel_id", "channel", "Channel", None),
                F("goodbye_message", "textarea", "Message", "**{user.name}** left the server. We now have {member_count} members.", max_length=2000),
                F("goodbye_embed", "bool", "Send as embed", True),
            ),
            section(
                "Auto roles",
                F("autorole_ids", "roles", "Roles given to new members", []),
                F("bot_autorole_ids", "roles", "Roles given to new bots", []),
                F("autorole_delay_seconds", "int", "Delay before giving roles (seconds)", 0, min=0, max=3600),
            ),
        ],
    },
    "leveling": {
        "title": "Leveling",
        "icon": "chart",
        "description": "XP for chatting, level-up messages and role rewards. Placeholders: {user} {level} {xp}",
        "sections": [
            section(
                "XP",
                F("enabled", "bool", "Leveling enabled", False),
                F("xp_min", "int", "Min XP per message", 15, min=1, max=1000),
                F("xp_max", "int", "Max XP per message", 25, min=1, max=1000),
                F("cooldown_seconds", "int", "XP cooldown (seconds)", 60, min=0, max=3600),
                F("no_xp_channel_ids", "channels", "No-XP channels", []),
                F("no_xp_role_ids", "roles", "No-XP roles", []),
                F(
                    "multipliers",
                    "rows",
                    "Role XP multipliers",
                    [],
                    columns=[F("role_id", "role", "Role", None), F("multiplier", "float", "Multiplier", 1.5, min=0, max=10)],
                    max_rows=25,
                ),
            ),
            section(
                "Level up",
                F("announce", "bool", "Announce level ups", True),
                F("announce_channel_id", "channel", "Announcement channel (empty = same channel)", None),
                F("levelup_message", "textarea", "Level-up message", "GG {user}, you reached **level {level}**!", max_length=1000),
            ),
            section(
                "Role rewards",
                F("stack_rewards", "bool", "Keep previous reward roles", True),
                F(
                    "rewards",
                    "rows",
                    "Rewards",
                    [],
                    columns=[F("level", "int", "Level", 5, min=1, max=1000), F("role_id", "role", "Role", None)],
                    max_rows=50,
                ),
            ),
        ],
    },
    "economy": {
        "title": "Economy",
        "icon": "coins",
        "description": "Virtual currency, rewards and the server shop.",
        "sections": [
            section(
                "Currency",
                F("enabled", "bool", "Economy enabled", False),
                F("currency_name", "text", "Currency name", "coins", max_length=32),
                F("currency_symbol", "text", "Currency symbol", "🪙", max_length=16),
                F("start_balance", "int", "Starting balance", 0, min=0, max=1_000_000_000),
            ),
            section(
                "Rewards",
                F("daily_amount", "int", "Daily reward", 100, min=0, max=1_000_000),
                F("daily_streak_bonus", "int", "Bonus per streak day", 10, min=0, max=100_000),
                F("weekly_amount", "int", "Weekly reward", 750, min=0, max=10_000_000),
                F("work_min", "int", "Work min payout", 20, min=0, max=1_000_000),
                F("work_max", "int", "Work max payout", 80, min=0, max=1_000_000),
                F("work_cooldown_minutes", "int", "Work cooldown (minutes)", 60, min=1, max=10080),
                F("message_reward", "int", "Coins per XP-eligible message", 0, min=0, max=1000),
            ),
        ],
    },
    "suggestions": {
        "title": "Suggestions",
        "icon": "lightbulb",
        "description": "Community suggestions with voting and staff responses.",
        "sections": [
            section(
                "General",
                F("enabled", "bool", "Suggestions enabled", False),
                F("channel_id", "channel", "Suggestions channel", None),
                F("create_threads", "bool", "Create a discussion thread per suggestion", True),
                F("anonymous", "bool", "Hide suggestion authors", False),
                F("dm_on_response", "bool", "DM author when staff respond", True),
            ),
        ],
    },
    "starboard": {
        "title": "Starboard",
        "icon": "star",
        "description": "Highlight popular messages.",
        "sections": [
            section(
                "General",
                F("enabled", "bool", "Starboard enabled", False),
                F("channel_id", "channel", "Starboard channel", None),
                F("emoji", "text", "Emoji", "⭐", max_length=64),
                F("threshold", "int", "Reactions required", 3, min=1, max=100),
                F("allow_self_star", "bool", "Count the author's own star", False),
                F("ignored_channel_ids", "channels", "Ignored channels", []),
            ),
        ],
    },
    "counting": {
        "title": "Counting",
        "icon": "hash",
        "description": "A counting game channel.",
        "sections": [
            section(
                "General",
                F("enabled", "bool", "Counting enabled", False),
                F("channel_id", "channel", "Counting channel", None),
                F("allow_consecutive", "bool", "Allow the same user to count twice in a row", False),
                F("reset_on_fail", "bool", "Reset to 0 on a wrong number", True),
                F("delete_wrong", "bool", "Delete wrong numbers instead of resetting", False),
            ),
        ],
    },
    "birthdays": {
        "title": "Birthdays",
        "icon": "cake",
        "description": "Birthday announcements. Placeholders: {user} {user.name}",
        "sections": [
            section(
                "General",
                F("enabled", "bool", "Birthdays enabled", False),
                F("channel_id", "channel", "Announcement channel", None),
                F("role_id", "role", "Birthday role (removed next day)", None),
                F("message", "textarea", "Message", "🎂 Happy birthday {user}!", max_length=1000),
                F("announce_hour_utc", "int", "Announce at hour (UTC)", 12, min=0, max=23),
            ),
        ],
    },
    "server_stats": {
        "title": "Server Stats Channels",
        "icon": "activity",
        "description": "Voice channels whose names show live counts (refreshed every 10 minutes due to Discord rate limits).",
        "sections": [
            section(
                "Counters",
                F("enabled", "bool", "Enabled", False),
                F("members_channel_id", "voice_channel", "Members channel", None),
                F("members_format", "text", "Members format", "Members: {count}", max_length=90),
                F("humans_channel_id", "voice_channel", "Humans channel", None),
                F("humans_format", "text", "Humans format", "Humans: {count}", max_length=90),
                F("bots_channel_id", "voice_channel", "Bots channel", None),
                F("bots_format", "text", "Bots format", "Bots: {count}", max_length=90),
                F("boosts_channel_id", "voice_channel", "Boosts channel", None),
                F("boosts_format", "text", "Boosts format", "Boosts: {count}", max_length=90),
            ),
        ],
    },
    "ai": {
        "title": "AI Assistant",
        "icon": "sparkles",
        "description": "Optional /ask command powered by Claude. Requires ANTHROPIC_API_KEY in .env (the bot owner's key).",
        "sections": [
            section(
                "General",
                F("enabled", "bool", "AI assistant enabled", False),
                F("allowed_channel_ids", "channels", "Allowed channels (empty = all)", []),
                F("allowed_role_ids", "roles", "Allowed roles (empty = everyone)", []),
                F("cooldown_seconds", "int", "Per-user cooldown (seconds)", 30, min=10, max=3600),
                F("system_prompt", "textarea", "Extra instructions", "You are Nexora, a friendly assistant for this Discord server. Keep answers short.", max_length=2000),
            ),
        ],
    },
}


def iter_fields(module: str):
    for sec in MODULES[module]["sections"]:
        yield from sec["fields"]


def defaults(module: str) -> dict:
    return {f["key"]: _copy(f["default"]) for f in iter_fields(module)}


def _copy(v):
    if isinstance(v, list):
        return [dict(x) if isinstance(x, dict) else x for x in v]
    if isinstance(v, dict):
        return dict(v)
    return v


class ValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _coerce(field: dict, value: Any) -> Any:
    t = field["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"1", "true", "on", "yes"}
        return bool(value)
    if t in {"int", "float"}:
        if value in (None, ""):
            raise ValueError("a number is required")
        num = int(value) if t == "int" else float(value)
        if "min" in field and num < field["min"]:
            raise ValueError(f"must be at least {field['min']}")
        if "max" in field and num > field["max"]:
            raise ValueError(f"must be at most {field['max']}")
        return num
    if t in {"text", "textarea"}:
        s = "" if value is None else str(value)
        if len(s) > field.get("max_length", 4000):
            raise ValueError(f"must be at most {field.get('max_length', 4000)} characters")
        return s
    if t == "url":
        s = (value or "").strip()
        if s and not (s.startswith("https://") or s.startswith("http://")):
            raise ValueError("must start with http:// or https://")
        return s[:1000]
    if t == "color":
        s = str(value or "").strip()
        if not s.startswith("#"):
            s = "#" + s
        int(s[1:], 16)
        if len(s) != 7:
            raise ValueError("must be a hex color like #7C3AED")
        return s.upper()
    if t in {"channel", "role", "voice_channel", "category"}:
        if value in (None, "", 0, "0"):
            return None
        return int(value)
    if t in {"channels", "roles"}:
        if not value:
            return []
        if isinstance(value, str):
            value = [v for v in value.split(",") if v.strip()]
        return list(dict.fromkeys(int(v) for v in value))[:100]
    if t == "string_list":
        if isinstance(value, str):
            value = value.replace("\n", ",").split(",")
        out = [str(v).strip().lower() for v in (value or []) if str(v).strip()]
        return list(dict.fromkeys(out))[:500]
    if t == "select":
        allowed = {o[0] for o in field["options"]}
        if value not in allowed:
            raise ValueError("invalid option")
        return value
    if t == "rows":
        rows = value or []
        if not isinstance(rows, list):
            raise ValueError("must be a list")
        if len(rows) > field.get("max_rows", 50):
            raise ValueError(f"at most {field.get('max_rows', 50)} rows")
        out = []
        for row in rows:
            clean = {}
            for col in field["columns"]:
                clean[col["key"]] = _coerce(col, (row or {}).get(col["key"], col["default"]))
            out.append(clean)
        return out
    raise ValueError(f"unknown field type {t}")


def validate(module: str, data: dict, partial: bool = True) -> dict:
    """Validate & coerce a (partial) settings update. Unknown keys are rejected."""
    if module not in MODULES:
        raise ValidationError({"module": "unknown module"})
    fields = {f["key"]: f for f in iter_fields(module)}
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    for key, value in data.items():
        field = fields.get(key)
        if not field:
            errors[key] = "unknown setting"
            continue
        try:
            clean[key] = _coerce(field, value)
        except (ValueError, TypeError) as exc:
            errors[key] = str(exc) or "invalid value"
    if not partial:
        for key, field in fields.items():
            clean.setdefault(key, _copy(field["default"]))
    if errors:
        raise ValidationError(errors)
    return clean
