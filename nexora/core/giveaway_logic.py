"""Pure giveaway helpers (testable without Discord)."""
from __future__ import annotations

import random


def entries_for(role_ids: set[int], bonus_roles: dict[str, int] | dict[int, int]) -> int:
    """1 base entry + the single highest bonus among the member's roles (bonuses don't stack)."""
    best = 0
    for rid, extra in (bonus_roles or {}).items():
        if int(rid) in role_ids:
            best = max(best, int(extra))
    return 1 + best


def pick_winners(entries: dict[int, int], count: int, exclude: set[int] | None = None, rng: random.Random | None = None) -> list[int]:
    """Weighted sampling without replacement."""
    rng = rng or random.SystemRandom()
    pool = {uid: max(1, w) for uid, w in entries.items() if uid not in (exclude or set())}
    winners: list[int] = []
    while pool and len(winners) < count:
        total = sum(pool.values())
        r = rng.uniform(0, total)
        upto = 0.0
        for uid, w in pool.items():
            upto += w
            if r <= upto:
                winners.append(uid)
                del pool[uid]
                break
        else:  # floating point edge
            uid = next(iter(pool))
            winners.append(uid)
            del pool[uid]
    return winners
