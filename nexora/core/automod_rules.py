"""Pure automod detection logic (no Discord I/O) so it can be unit tested."""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import urlparse

INVITE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg)/([\w-]+)", re.I)
URL_RE = re.compile(r"https?://[^\s<>()]+", re.I)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w{1,32}:\d{15,25}>")
UNICODE_EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF☀-⛿✀-➿]"
)

OFFICIAL_DOMAINS = {
    "discord.com", "discord.gg", "discordapp.com", "discordapp.net", "discord.media", "discord.gift",
    "discordstatus.com", "discord.dev", "discord.new", "steampowered.com", "steamcommunity.com",
    "store.steampowered.com", "help.steampowered.com",
}
# Typosquat / scam fragments commonly seen in Discord phishing links.
SCAM_FRAGMENTS = [
    "dlscord", "disc0rd", "discorcl", "dicsord", "discrod", "disocrd", "discordn", "discord-nitro", "discordnitro",
    "nitro-gift", "free-nitro", "freenitro", "discord-gift", "discordgift", "discord-app", "steamcommunlty",
    "steamcomunity", "stearncommunity", "steamcommnuity", "steam-gift", "steamgift", "csgo-skin", "grabify",
    "iplogger", "2no.co", "blasze", "yip.su",
]


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def domain_matches(host: str, allowed: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in allowed)


def find_invites(content: str) -> list[str]:
    return INVITE_RE.findall(content)


def find_links(content: str) -> list[str]:
    return URL_RE.findall(content)


def is_malicious(url: str, extra: list[str]) -> bool:
    host = domain_of(url)
    if not host:
        return False
    if domain_matches(host, list(OFFICIAL_DOMAINS)):
        return False
    if extra and domain_matches(host, extra):
        return True
    if any(frag in host for frag in SCAM_FRAGMENTS):
        return True
    # brand names used on non-official domains (e.g. discord-airdrop.xyz)
    return any(brand in host for brand in ("discord", "steamcommunity", "nitro"))


def find_profanity(content: str, words: list[str], wildcard: bool) -> str | None:
    lowered = content.lower()
    for w in words:
        if not w:
            continue
        if wildcard:
            if w in lowered:
                return w
        elif re.search(rf"(?<!\w){re.escape(w)}(?!\w)", lowered):
            return w
    return None


def count_emojis(content: str) -> int:
    return len(CUSTOM_EMOJI_RE.findall(content)) + len(UNICODE_EMOJI_RE.findall(CUSTOM_EMOJI_RE.sub("", content)))


def caps_ratio(content: str) -> float:
    letters = [c for c in content if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def blocked_attachment(filenames: list[str], blocked_ext: list[str]) -> str | None:
    blocked = {e.lower().lstrip(".") for e in blocked_ext}
    for name in filenames:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in blocked:
            return name
    return None


@dataclass
class Violation:
    rule: str
    detail: str


class RateTracker:
    """Sliding-window counters for spam/raid detection."""

    def __init__(self):
        self.events: dict[tuple, deque[float]] = defaultdict(deque)

    def hit(self, key: tuple, window: float, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        q = self.events[key]
        q.append(now)
        while q and now - q[0] > window:
            q.popleft()
        return len(q)

    def reset(self, key: tuple) -> None:
        self.events.pop(key, None)


class RepeatTracker:
    def __init__(self):
        self.last: dict[tuple, tuple[str, int]] = {}

    def hit(self, key: tuple, content: str) -> int:
        norm = " ".join(content.lower().split())
        if not norm:
            return 0
        prev, count = self.last.get(key, ("", 0))
        count = count + 1 if prev == norm else 1
        self.last[key] = (norm, count)
        return count


def check_message(s: dict, content: str, mention_count: int, attachment_names: list[str], own_invite_codes: set[str],
                  spam_count: int | None, repeat_count: int | None) -> Violation | None:
    """Evaluate all content rules in priority order. spam/repeat counts are pre-computed by the caller."""
    if s["malicious_enabled"]:
        for url in find_links(content):
            if is_malicious(url, s["malicious_domains"]):
                return Violation("malicious", f"Malicious link: {domain_of(url)}")
    if s["spam_enabled"] and spam_count is not None and spam_count > s["spam_max_messages"]:
        return Violation("spam", f"{spam_count} messages in {s['spam_seconds']}s")
    if s["repeat_enabled"] and repeat_count is not None and repeat_count >= s["repeat_count"]:
        return Violation("repeat", f"Same message sent {repeat_count} times")
    if s["invites_enabled"]:
        codes = find_invites(content)
        bad = [c for c in codes if not (s["invites_allow_own"] and c in own_invite_codes)]
        if bad:
            return Violation("invites", f"Invite link: {bad[0]}")
    if s["links_enabled"]:
        for url in find_links(content):
            host = domain_of(url)
            if host and not domain_matches(host, s["links_allowed_domains"]) and not INVITE_RE.match(url):
                return Violation("links", f"Link to {host}")
    if s["profanity_enabled"] and s["profanity_words"]:
        word = find_profanity(content, s["profanity_words"], s["profanity_wildcard"])
        if word:
            return Violation("profanity", "Blocked word")
    if s["mentions_enabled"] and mention_count > s["mentions_max"]:
        return Violation("mentions", f"{mention_count} mentions")
    if s["emoji_enabled"]:
        n = count_emojis(content)
        if n > s["emoji_max"]:
            return Violation("emoji", f"{n} emojis")
    if s["caps_enabled"] and len(content) > s["caps_min_length"]:
        ratio = caps_ratio(content)
        if ratio * 100 > s["caps_percent"]:
            return Violation("caps", f"{ratio:.0%} capitals")
    if s["attachments_enabled"] and attachment_names:
        name = blocked_attachment(attachment_names, s["attachments_blocked_ext"])
        if name:
            return Violation("attachments", f"Blocked file: {name}")
    return None
