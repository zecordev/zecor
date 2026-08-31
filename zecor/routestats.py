# SPDX-License-Identifier: Apache-2.0
"""Learned per-tier success rates for expected-value routing -- build-queue item G.

Opt-in behind `ZECOR_EV_ROUTING=1`: EV = P(land | tier) - COST_WEIGHT x cost picks the
order of untried frontier rungs; `route()` stays deterministic by default. Success rate
is a Beta(1, 1) posterior mean, so a tier with no history sits at 0.5 rather than 0 or
1 -- the ladder's own order breaks ties.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

# Relative cost per rung. T1/T2b are free (local / included plan); T3 and T4 burn a
# metered or capped pool, Opus about twice a Sonnet call.
COST = {
    ("t1", None): 0.0,
    ("t2b", None): 0.0,
    ("t3", "gpt-5.6-terra"): 1.0,
    ("t3", None): 1.0,
    ("t4", "claude-sonnet-5"): 2.0,
    ("t4", "claude-opus-5"): 4.0,
    ("t4", None): 2.0,
}
COST_WEIGHT = 0.08  # a rung is worth ~12pp of land-rate per unit of cost


def enabled() -> bool:
    return os.environ.get("ZECOR_EV_ROUTING", "").strip() not in ("", "0", "false", "no")


@dataclass(frozen=True)
class TierStat:
    trials: int
    wins: int

    def land_rate(self) -> float:
        return (self.wins + 1) / (self.trials + 2)  # Beta(1,1) posterior mean


Key = tuple[str, str | None]


def from_rows(rows: list[tuple[str, str | None, str]]) -> dict[Key, TierStat]:
    """rows = (tier, model, outcome). A 'pass' outcome is a win."""
    agg: dict[Key, list[int]] = {}
    for tier, model, outcome in rows:
        t = agg.setdefault((tier, model), [0, 0])
        t[0] += 1
        if outcome == "pass":
            t[1] += 1
    return {k: TierStat(v[0], v[1]) for k, v in agg.items()}


def read(db: sqlite3.Connection) -> dict[Key, TierStat]:
    try:
        cur = db.execute("SELECT tier, model, outcome FROM attempt")
        rows = [(str(r[0]), r[1], str(r[2])) for r in cur.fetchall()]
    except sqlite3.Error:
        return {}
    return from_rows(rows)


def ev(key: Key, stats: dict[Key, TierStat]) -> float:
    rate = stats.get(key, TierStat(0, 0)).land_rate()
    cost = COST.get(key, COST.get((key[0], None), 1.0))
    return rate - COST_WEIGHT * cost


def order_by_ev(ladder: list[Key], stats: dict[Key, TierStat]) -> list[Key]:
    """Stable-sort the ladder by descending EV -- ties keep the ladder's own order, so
    with no history this is a no-op."""
    return sorted(ladder, key=lambda k: -ev(k, stats))