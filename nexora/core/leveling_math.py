"""XP curve helpers (same shape as the popular MEE6 curve)."""
from __future__ import annotations


def xp_for_next(level: int) -> int:
    """XP needed to go from `level` to `level + 1`."""
    return 5 * level * level + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    return sum(xp_for_next(lv) for lv in range(level))


def level_from_xp(xp: int) -> int:
    level = 0
    while xp >= xp_for_next(level):
        xp -= xp_for_next(level)
        level += 1
    return level


def progress(xp: int) -> tuple[int, int, int]:
    """Return (level, xp_into_level, xp_needed_for_level)."""
    level = level_from_xp(xp)
    into = xp - total_xp_for_level(level)
    return level, into, xp_for_next(level)


def progress_bar(fraction: float, width: int = 14) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)
