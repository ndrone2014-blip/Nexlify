"""Automation definitions + pure condition evaluation (testable without Discord)."""
from __future__ import annotations

from typing import Any

TRIGGERS: dict[str, str] = {
    "member_join": "Member joins",
    "member_leave": "Member leaves",
    "role_added": "Member receives a role",
    "role_removed": "Member loses a role",
    "message_sent": "Member sends a message",
    "ticket_opened": "Ticket opens",
    "ticket_closed": "Ticket closes",
    "application_submitted": "Application submitted",
    "application_accepted": "Application accepted",
    "application_denied": "Application denied",
    "giveaway_ended": "Giveaway ends",
    "level_up": "Member levels up",
    "member_warned": "Member is warned",
    "suggestion_created": "Suggestion posted",
    "raid_detected": "Raid detected",
}

CONDITIONS: dict[str, dict] = {
    "has_role": {"label": "Member has role", "value": "role"},
    "lacks_role": {"label": "Member does not have role", "value": "role"},
    "role_is": {"label": "The added/removed role is", "value": "role"},
    "channel_is": {"label": "Channel is", "value": "channel"},
    "content_contains": {"label": "Message contains text", "value": "text"},
    "content_starts_with": {"label": "Message starts with", "value": "text"},
    "level_at_least": {"label": "Level is at least", "value": "int"},
    "level_equals": {"label": "Level equals", "value": "int"},
    "account_younger_than_days": {"label": "Account younger than (days)", "value": "int"},
    "warnings_at_least": {"label": "Active warnings at least", "value": "int"},
    "is_bot": {"label": "Member is a bot", "value": "bool"},
}

ACTIONS: dict[str, dict] = {
    "send_message": {"label": "Send a message", "fields": ["channel", "content"]},
    "send_embed": {"label": "Send an embed", "fields": ["channel", "title", "content", "color"]},
    "dm_member": {"label": "DM the member", "fields": ["content"]},
    "add_role": {"label": "Assign a role", "fields": ["role"]},
    "remove_role": {"label": "Remove a role", "fields": ["role"]},
    "create_log": {"label": "Create a log entry", "fields": ["content"]},
    "notify_staff": {"label": "Notify staff (pings staff roles)", "fields": ["channel", "content"]},
    "create_ticket": {"label": "Create a ticket for the member", "fields": ["panel"]},
    "add_xp": {"label": "Give XP", "fields": ["amount"]},
    "give_currency": {"label": "Give currency", "fields": ["amount"]},
    "timeout_member": {"label": "Time out the member (minutes)", "fields": ["amount"]},
}

MAX_CONDITIONS = 10
MAX_ACTIONS = 10


def validate_automation(data: dict) -> dict:
    """Validate a dashboard submission. Raises ValueError with a readable message."""
    name = str(data.get("name", "")).strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("Name must be 1-80 characters")
    trigger = data.get("trigger")
    if trigger not in TRIGGERS:
        raise ValueError("Unknown trigger")
    conditions = []
    for c in (data.get("conditions") or [])[:MAX_CONDITIONS]:
        ctype = c.get("type")
        if ctype not in CONDITIONS:
            raise ValueError(f"Unknown condition {ctype}")
        kind = CONDITIONS[ctype]["value"]
        val = c.get("value")
        if kind in {"role", "channel", "int"}:
            try:
                val = int(val)
            except (TypeError, ValueError):
                raise ValueError(f"Condition '{CONDITIONS[ctype]['label']}' needs a number/selection") from None
        elif kind == "bool":
            val = val in (True, "true", "1", 1, "on")
        else:
            val = str(val or "")[:200]
            if not val:
                raise ValueError(f"Condition '{CONDITIONS[ctype]['label']}' needs text")
        conditions.append({"type": ctype, "value": val})
    actions = []
    for a in (data.get("actions") or [])[:MAX_ACTIONS]:
        atype = a.get("type")
        if atype not in ACTIONS:
            raise ValueError(f"Unknown action {atype}")
        clean: dict[str, Any] = {"type": atype}
        for f in ACTIONS[atype]["fields"]:
            v = a.get(f)
            if f in {"channel", "role", "panel"}:
                try:
                    clean[f] = int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"Action '{ACTIONS[atype]['label']}' needs a {f}") from None
            elif f == "amount":
                try:
                    clean[f] = max(-1_000_000, min(1_000_000, int(v)))
                except (TypeError, ValueError):
                    raise ValueError(f"Action '{ACTIONS[atype]['label']}' needs a number") from None
            else:
                clean[f] = str(v or "")[:2000]
        if atype in {"send_message", "dm_member", "notify_staff", "create_log"} and not clean.get("content"):
            raise ValueError(f"Action '{ACTIONS[atype]['label']}' needs content")
        actions.append(clean)
    if not actions:
        raise ValueError("Add at least one action")
    return {"name": name, "trigger": trigger, "conditions": conditions, "actions": actions, "enabled": bool(data.get("enabled", True))}


def check_conditions(conditions: list[dict], ctx: dict) -> tuple[bool, str]:
    """ctx keys: role_ids(set), role_id, channel_id, content, level, account_age_days, warnings, is_bot."""
    for c in conditions:
        t, v = c["type"], c["value"]
        ok = True
        if t == "has_role":
            ok = v in ctx.get("role_ids", set())
        elif t == "lacks_role":
            ok = v not in ctx.get("role_ids", set())
        elif t == "role_is":
            ok = ctx.get("role_id") == v
        elif t == "channel_is":
            ok = ctx.get("channel_id") == v
        elif t == "content_contains":
            ok = str(v).lower() in (ctx.get("content") or "").lower()
        elif t == "content_starts_with":
            ok = (ctx.get("content") or "").lower().startswith(str(v).lower())
        elif t == "level_at_least":
            ok = (ctx.get("level") or 0) >= v
        elif t == "level_equals":
            ok = (ctx.get("level") or 0) == v
        elif t == "account_younger_than_days":
            ok = ctx.get("account_age_days") is not None and ctx["account_age_days"] < v
        elif t == "warnings_at_least":
            ok = (ctx.get("warnings") or 0) >= v
        elif t == "is_bot":
            ok = bool(ctx.get("is_bot")) == bool(v)
        if not ok:
            return False, f"condition '{CONDITIONS[t]['label']}' not met"
    return True, "ok"
