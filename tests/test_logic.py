"""Unit tests for pure logic: schema validation, automod rules, giveaways, leveling, automation, helpers."""
import random
from datetime import timedelta

import pytest

from nexora.core import schema
from nexora.core.automation_engine import check_conditions, validate_automation
from nexora.core.automod_rules import (RateTracker, RepeatTracker, blocked_attachment, caps_ratio, check_message, count_emojis,
                                       find_invites, find_profanity, is_malicious)
from nexora.core.giveaway_logic import entries_for, pick_winners
from nexora.core.leveling_math import level_from_xp, progress, total_xp_for_level, xp_for_next
from nexora.core.utils import embed_from_dict, embed_has_content, human_duration, ordinal, parse_duration
from nexora.bot.cogs.applications import chunk_steps


# ------------------------------------------------------------------ schema
def test_every_module_has_valid_defaults():
    for module in schema.MODULES:
        d = schema.defaults(module)
        assert schema.validate(module, d, partial=False) == d, module


def test_field_keys_unique_per_module():
    for module in schema.MODULES:
        keys = [f["key"] for f in schema.iter_fields(module)]
        assert len(keys) == len(set(keys)), module


def test_validate_coerces_and_rejects():
    out = schema.validate("leveling", {"xp_min": "20", "enabled": "true", "no_xp_channel_ids": ["123", "123", "456"]})
    assert out == {"xp_min": 20, "enabled": True, "no_xp_channel_ids": [123, 456]}
    with pytest.raises(schema.ValidationError) as e:
        schema.validate("leveling", {"xp_min": 0, "bogus": 1, "announce_channel_id": "abc"})
    assert set(e.value.errors) == {"xp_min", "bogus", "announce_channel_id"}


def test_validate_rows_and_select():
    rows = schema.validate("moderation", {"escalation": [{"warnings": "2", "action": "kick", "duration_minutes": 0}]})["escalation"]
    assert rows == [{"warnings": 2, "action": "kick", "duration_minutes": 0}]
    with pytest.raises(schema.ValidationError):
        schema.validate("moderation", {"escalation": [{"warnings": 2, "action": "explode"}]})
    with pytest.raises(schema.ValidationError):
        schema.validate("general", {"embed_color": "#12345"})
    assert schema.validate("general", {"embed_color": "abcdef"})["embed_color"] == "#ABCDEF"
    with pytest.raises(schema.ValidationError):
        schema.validate("welcome", {"welcome_image_url": "javascript:alert(1)"})


# ----------------------------------------------------------------- automod
AUTOMOD = {**schema.defaults("automod"), **{k: True for k in schema.defaults("automod") if k.endswith("_enabled") and k not in {"raid_enabled", "newaccount_enabled"}}}
AUTOMOD["profanity_words"] = ["badword"]


def test_invite_detection_and_own_invites():
    assert find_invites("join discord.gg/abc123 now") == ["abc123"]
    assert find_invites("https://discord.com/invite/xyz") == ["xyz"]
    v = check_message(AUTOMOD, "discord.gg/abc123", 0, [], {"abc123"}, 1, 1)
    assert v is None  # own invite allowed
    v = check_message(AUTOMOD, "discord.gg/other", 0, [], {"abc123"}, 1, 1)
    assert v.rule == "invites"


def test_malicious_links():
    assert is_malicious("https://dlscord-nitro.gift/claim", [])
    assert is_malicious("https://steamcommunlty.com/trade", [])
    assert not is_malicious("https://discord.com/channels/1/2", [])
    assert not is_malicious("https://youtube.com/watch?v=1", [])
    assert is_malicious("https://evil.example/x", ["evil.example"])
    assert check_message(AUTOMOD, "free nitro https://discord-airdrop.xyz/", 0, [], set(), 1, 1).rule == "malicious"


def test_links_allowlist():
    assert check_message(AUTOMOD, "see https://www.youtube.com/watch?v=1", 0, [], set(), 1, 1) is None
    assert check_message(AUTOMOD, "see https://example.org/page", 0, [], set(), 1, 1).rule == "links"


def test_profanity_word_boundaries():
    assert find_profanity("this is a BadWord!", ["badword"], False) == "badword"
    assert find_profanity("notbadwordy", ["badword"], False) is None
    assert find_profanity("notbadwordy", ["badword"], True) == "badword"


def test_mentions_emoji_caps_attachments():
    assert check_message(AUTOMOD, "hi", 9, [], set(), 1, 1).rule == "mentions"
    assert count_emojis("😀😀 <:pog:123456789012345678> <a:x:123456789012345679>") == 4
    assert check_message(AUTOMOD, "😀" * 12, 0, [], set(), 1, 1).rule == "emoji"
    assert caps_ratio("HELLO world") == pytest.approx(0.5)
    assert check_message(AUTOMOD, "WHY IS EVERYONE SO LOUD", 0, [], set(), 1, 1).rule == "caps"
    assert check_message(AUTOMOD, "OK", 0, [], set(), 1, 1) is None  # too short for caps rule
    assert blocked_attachment(["photo.png", "virus.EXE"], ["exe"]) == "virus.EXE"
    assert check_message(AUTOMOD, "", 0, ["setup.bat"], set(), 1, 1).rule == "attachments"


def test_spam_and_repeat_trackers():
    rt = RateTracker()
    counts = [rt.hit(("k",), 5, now=t) for t in (0, 1, 2, 3, 4, 10)]
    assert counts == [1, 2, 3, 4, 5, 1]
    rep = RepeatTracker()
    assert [rep.hit(("u",), m) for m in ("hi", "HI ", "hi", "yo")] == [1, 2, 3, 1]
    assert check_message(AUTOMOD, "x", 0, [], set(), 7, 1).rule == "spam"
    assert check_message(AUTOMOD, "same", 0, [], set(), 1, 3).rule == "repeat"


def test_disabled_rules_do_nothing():
    off = schema.defaults("automod")
    assert check_message(off, "discord.gg/x https://dlscord.gift BADWORD 😀😀😀😀😀😀😀😀😀😀😀😀", 50, ["a.exe"], set(), 99, 99) is None


# --------------------------------------------------------------- giveaways
def test_bonus_entries_take_highest_not_sum():
    assert entries_for({1, 2}, {"1": 2, "2": 5}) == 6
    assert entries_for({3}, {"1": 2}) == 1


def test_pick_winners_unique_and_excludes():
    rng = random.Random(42)
    pool = {i: 1 for i in range(20)}
    w = pick_winners(pool, 5, exclude={0, 1}, rng=rng)
    assert len(w) == len(set(w)) == 5 and not ({0, 1} & set(w))
    assert pick_winners({1: 1}, 3) == [1]
    assert pick_winners({}, 3) == []


def test_pick_winners_respects_weights():
    rng = random.Random(1)
    wins = sum(pick_winners({1: 99, 2: 1}, 1, rng=rng)[0] == 1 for _ in range(500))
    assert wins > 450


# ---------------------------------------------------------------- leveling
def test_leveling_curve_roundtrip():
    assert xp_for_next(0) == 100 and xp_for_next(1) == 155
    for lvl in (0, 1, 5, 20, 77):
        assert level_from_xp(total_xp_for_level(lvl)) == lvl
        assert level_from_xp(total_xp_for_level(lvl) - 1) == max(0, lvl - 1)
    lvl, into, need = progress(total_xp_for_level(3) + 10)
    assert (lvl, into, need) == (3, 10, xp_for_next(3))


# -------------------------------------------------------------- automation
def test_validate_automation():
    a = validate_automation({"name": "Lvl 10", "trigger": "level_up", "conditions": [{"type": "level_equals", "value": "10"}],
                             "actions": [{"type": "add_role", "role": "123"}, {"type": "send_message", "channel": "5", "content": "GG {user}"}]})
    assert a["conditions"][0]["value"] == 10 and a["actions"][0]["role"] == 123
    for bad in ({"name": "", "trigger": "level_up", "actions": [{"type": "add_xp", "amount": 1}]},
                {"name": "x", "trigger": "nope", "actions": [{"type": "add_xp", "amount": 1}]},
                {"name": "x", "trigger": "level_up", "actions": []},
                {"name": "x", "trigger": "level_up", "actions": [{"type": "send_message", "channel": "1", "content": ""}]},
                {"name": "x", "trigger": "level_up", "conditions": [{"type": "has_role", "value": "abc"}], "actions": [{"type": "add_xp", "amount": 1}]}):
        with pytest.raises(ValueError):
            validate_automation(bad)


def test_check_conditions():
    ctx = {"role_ids": {1, 2}, "level": 10, "content": "Hello World", "account_age_days": 2, "is_bot": False, "warnings": 1}
    assert check_conditions([{"type": "has_role", "value": 1}, {"type": "level_at_least", "value": 5}], ctx)[0]
    assert not check_conditions([{"type": "lacks_role", "value": 2}], ctx)[0]
    assert check_conditions([{"type": "content_contains", "value": "world"}], ctx)[0]
    assert check_conditions([{"type": "account_younger_than_days", "value": 7}], ctx)[0]
    assert not check_conditions([{"type": "is_bot", "value": True}], ctx)[0]
    assert check_conditions([], ctx) == (True, "ok")


# ----------------------------------------------------------------- helpers
def test_parse_and_format_durations():
    assert parse_duration("1d2h") == timedelta(days=1, hours=2)
    assert parse_duration("30") == timedelta(minutes=30)
    assert parse_duration("1w 3m") == timedelta(weeks=1, minutes=3)
    assert parse_duration("soon") is None and parse_duration("") is None
    assert human_duration(90061) == "1d 1h 1m"
    assert human_duration(None) == "permanent"
    assert [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 112)] == ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "112th"]


def test_embed_from_dict_limits():
    e = embed_from_dict({"title": "x" * 300, "description": "d", "color": "#FF0000", "fields": [{"name": "a", "value": "b", "inline": True}] * 30,
                         "footer": {"text": "f"}, "timestamp": True})
    assert len(e.title) == 256 and len(e.fields) == 25 and e.color.value == 0xFF0000 and e.timestamp
    assert not embed_has_content({"content": "hi", "buttons": [{"label": "a", "url": "https://x"}]})
    assert embed_has_content({"title": "t"})


def test_application_steps_respect_discord_limits():
    qs = [{"type": "short"}] * 7 + [{"type": "choice"}] + [{"type": "long"}] * 2
    assert chunk_steps(qs) == [[0, 1, 2, 3, 4], [5, 6], [7], [8, 9]]
    assert all(len(s) <= 5 for s in chunk_steps([{"type": "short"}] * 23))
