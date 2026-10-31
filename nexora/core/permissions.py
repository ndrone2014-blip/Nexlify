"""Shared permission rules used by both the dashboard and the bot.

The key rule: anything that makes Nexora *give out* a role later (role menus, shop items, level rewards,
autoroles, application rewards, automations, config imports) is checked against the person setting it up,
not just against Nexora. Otherwise someone with Manage Server could configure Nexora to hand them a role
Discord itself would never let them assign.
"""
from __future__ import annotations

import re
from typing import Iterable

import discord

# Settings fields whose roles Nexora hands out to members. ("rows" fields list the column holding the role.)
GRANT_ROLE_FIELDS: dict[str, dict[str, str | None]] = {
    "welcome": {"autorole_ids": None, "bot_autorole_ids": None},
    "leveling": {"rewards": "role_id"},
    "birthdays": {"role_id": None},
}

# Settings that decide who can use the dashboard. Only people with real Manage Server/Administrator
# (or the owner) may change them, so a delegated dashboard role can't hand dashboard access to others.
DASHBOARD_ACCESS_FIELDS = {"general": ("dashboard_role_ids", "dashboard_readonly_role_ids")}

MENTION_RE = re.compile(r"@everyone|@here")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


def is_native_manager(member: discord.Member | None) -> bool:
    """Owner, Administrator or Manage Server from Discord itself (not from a Nexora dashboard role)."""
    if member is None:
        return False
    p = member.guild_permissions
    return member.id == member.guild.owner_id or p.administrator or p.manage_guild


def role_grant_problem(actor: discord.Member | None, role: discord.Role) -> str | None:
    """Return why `actor` may not set Nexora up to give `role` to members, or None if it's fine."""
    guild = role.guild
    if role.is_default():
        return "@everyone can't be given out"
    me = guild.me
    if role.managed or (me and role >= me.top_role):
        return f"Nexora can't assign @{role.name} (it's above Nexora's role or managed)"
    if actor is None:
        return "Couldn't confirm your Discord permissions; try again in a moment"
    if actor.id == guild.owner_id:
        return None
    perms = actor.guild_permissions
    if not (perms.manage_roles or perms.administrator):
        return "You need the Manage Roles permission in Discord to set up roles that Nexora gives out"
    if role >= actor.top_role:
        return f"@{role.name} is equal to or above your highest role, so you can't make Nexora give it out"
    if not perms.administrator and not role.permissions.is_subset(perms):
        return f"@{role.name} has permissions you don't have, so you can't make Nexora give it out"
    return None


def grant_problems(actor: discord.Member | None, guild: discord.Guild, role_ids: Iterable[int],
                   already: Iterable[int] = ()) -> list[str]:
    """Check every role in `role_ids` that isn't in `already` (roles that were set before are left alone,
    so a lower manager can still edit other parts of something a higher one set up)."""
    skip = {int(r) for r in already if r}
    out = []
    for rid in dict.fromkeys(int(r) for r in role_ids if r):
        if rid in skip:
            continue
        role = guild.get_role(rid)
        if role is None:
            continue  # unknown roles are ignored by the bot at runtime
        problem = role_grant_problem(actor, role)
        if problem:
            out.append(problem)
    return out


def settings_grant_roles(module: str, values: dict) -> dict[str, set[int]]:
    """Role IDs per granting field, for the keys present in `values`."""
    out: dict[str, set[int]] = {}
    for key, col in GRANT_ROLE_FIELDS.get(module, {}).items():
        if key not in values:
            continue
        v = values[key]
        ids: set[int] = set()
        if col:
            for row in v or []:
                rid = (row or {}).get(col) if isinstance(row, dict) else None
                if rid and str(rid).isdigit():
                    ids.add(int(rid))
        elif isinstance(v, list):
            ids = {int(x) for x in v if x and str(x).isdigit()}
        elif v and str(v).isdigit():
            ids = {int(v)}
        out[key] = ids
    return out


def settings_problems(actor: discord.Member | None, guild: discord.Guild, module: str, new: dict, current: dict) -> dict[str, str]:
    """Permission problems for a settings update, keyed by setting name (same shape as validation errors)."""
    errors: dict[str, str] = {}
    old_roles = settings_grant_roles(module, current)
    for key, ids in settings_grant_roles(module, new).items():
        problems = grant_problems(actor, guild, ids, old_roles.get(key, set()))
        if problems:
            errors[key] = problems[0]
    for key in DASHBOARD_ACCESS_FIELDS.get(module, ()):
        if key in new and not is_native_manager(actor):
            old = {int(x) for x in current.get(key) or []}
            given = {int(x) for x in new.get(key) or [] if str(x).isdigit()}
            if old != given:
                errors[key] = "Only the server owner or members with Manage Server can change dashboard access"
    return errors


def can_mention_everyone(actor: discord.Member | None) -> bool:
    if actor is None:
        return False
    return actor.id == actor.guild.owner_id or actor.guild_permissions.administrator or actor.guild_permissions.mention_everyone


def mention_problem(actor: discord.Member | None, guild: discord.Guild, *texts: str | None) -> str | None:
    """Stop people from making Nexora ping @everyone/@here or locked roles when they couldn't themselves."""
    if can_mention_everyone(actor):
        return None
    text = "\n".join(t for t in texts if t)
    if MENTION_RE.search(text):
        return "You need the Mention @everyone permission in Discord to ping @everyone or @here"
    for rid in ROLE_MENTION_RE.findall(text):
        role = guild.get_role(int(rid))
        if role and not role.mentionable:
            return f"You need the Mention @everyone permission to ping @{role.name}"
    return None


def allowed_mentions_for(actor: discord.Member | None, guild: discord.Guild | None) -> discord.AllowedMentions:
    """Pings Nexora may send on someone's behalf: everything if they could ping it themselves,
    otherwise only users and roles anyone is allowed to mention."""
    if can_mention_everyone(actor):
        return discord.AllowedMentions(everyone=True, roles=True, users=True)
    roles = [r for r in guild.roles if r.mentionable and not r.is_default()][:100] if guild else []
    return discord.AllowedMentions(everyone=False, roles=roles or False, users=True)
