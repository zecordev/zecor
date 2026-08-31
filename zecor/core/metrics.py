# SPDX-License-Identifier: Apache-2.0
"""Is this working? -- Console II, bucket B (data-dependent).

Pure reads over the `attempt` ledger: land rate per tier, cycle time, a rough cost
estimate, and a daily-lands series. Every function returns a clean "nothing yet" shape
on an empty ledger and stays correct once it fills; tests seed synthetic rows since
real ledger data doesn't exist yet.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from zecor import config

_LAND = "pass"  # attempt.outcome for a landed attempt

# very rough USD per 1M tokens, for an order-of-magnitude "what did the night cost".
# local tiers are 0; unknown models fall through to 0 rather than guess high.
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {  # model substring -> (in, out)
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.80, 4.0),
    "gpt-4": (2.5, 10.0),
    "o1": (15.0, 60.0),
    "o3": (2.0, 8.0),
    "gemini-1.5-pro": (1.25, 5.0),
    "gemini-2": (1.25, 10.0),
}

_BARS = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True)
class TierStat:
    tier: str
    attempts: int
    lands: int

    @property
    def rate(self) -> float:
        return self.lands / self.attempts if self.attempts else 0.0


@dataclass(frozen=True)
class MetricsSummary:
    empty: bool
    total_attempts: int
    total_lands: int
    by_tier: list[TierStat] = field(default_factory=list)
    median_cycle_s: float | None = None
    p90_cycle_s: float | None = None
    est_cost_usd: float = 0.0
    daily_lands: list[int] = field(default_factory=list)


def _connect() -> sqlite3.Connection | None:
    if not config.DB_PATH.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5.0)
        db.row_factory = sqlite3.Row
        return db
    except sqlite3.Error:
        return None


def sparkline(values: list[int | float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _BARS[0] * len(values) if hi == 0 else _BARS[4] * len(values)
    span = hi - lo
    return "".join(_BARS[min(7, int((v - lo) / span * 7 + 0.5))] for v in values)


def landrate_by_tier() -> list[TierStat]:
    db = _connect()
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT tier, COUNT(*) AS n, "
            "SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END) AS lands "
            "FROM attempt GROUP BY tier ORDER BY tier",
            (_LAND,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [
        TierStat(str(r["tier"] or "?"), int(r["n"] or 0), int(r["lands"] or 0))
        for r in rows
    ]


def _cycle_seconds() -> list[float]:
    db = _connect()
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT started_at, ended_at FROM attempt "
            "WHERE outcome = ? AND started_at IS NOT NULL AND ended_at IS NOT NULL",
            (_LAND,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    out: list[float] = []
    for r in rows:
        try:
            a = datetime.fromisoformat(str(r["started_at"]))
            b = datetime.fromisoformat(str(r["ended_at"]))
        except ValueError:
            continue
        delta = (b - a).total_seconds()
        if delta >= 0:
            out.append(delta)
    return out


def _price(model: str | None) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in _PRICE_PER_MTOK.items():
        if key in m:
            return price
    return (0.0, 0.0)


def est_cost_usd(*, since: str | None = None) -> float:
    """Rough spend across the ledger, or only over attempts started at/after `since`
    (ISO8601) when given."""
    db = _connect()
    if db is None:
        return 0.0
    q = "SELECT model, SUM(in_tokens) AS itok, SUM(out_tokens) AS otok FROM attempt"
    args: tuple[object, ...] = ()
    if since is not None:
        q += " WHERE started_at >= ?"
        args = (since,)
    q += " GROUP BY model"
    try:
        rows = db.execute(q, args).fetchall()
    except sqlite3.Error:
        return 0.0
    finally:
        db.close()
    total = 0.0
    for r in rows:
        pin, pout = _price(r["model"])
        total += (int(r["itok"] or 0) / 1e6) * pin + (int(r["otok"] or 0) / 1e6) * pout
    return round(total, 2)


def daily_lands(days: int = 14, *, now: datetime | None = None) -> list[int]:
    end = (now or datetime.now(UTC)).date()
    start = end - timedelta(days=days - 1)
    buckets = {start + timedelta(days=i): 0 for i in range(days)}
    db = _connect()
    if db is None:
        return list(buckets.values())
    try:
        rows = db.execute(
            "SELECT started_at FROM attempt WHERE outcome = ? AND started_at IS NOT NULL",
            (_LAND,),
        ).fetchall()
    except sqlite3.Error:
        return list(buckets.values())
    finally:
        db.close()
    for r in rows:
        try:
            d = datetime.fromisoformat(str(r["started_at"])).date()
        except ValueError:
            continue
        if d in buckets:
            buckets[d] += 1
    return [buckets[k] for k in sorted(buckets)]


def summary(*, now: datetime | None = None) -> MetricsSummary:
    tiers = landrate_by_tier()
    total = sum(t.attempts for t in tiers)
    lands = sum(t.lands for t in tiers)
    cyc = _cycle_seconds()
    med = statistics.median(cyc) if cyc else None
    p90 = (statistics.quantiles(cyc, n=10)[-1] if len(cyc) >= 2 else (cyc[0] if cyc else None))
    return MetricsSummary(
        empty=total == 0,
        total_attempts=total,
        total_lands=lands,
        by_tier=tiers,
        median_cycle_s=med,
        p90_cycle_s=p90,
        est_cost_usd=est_cost_usd(),
        daily_lands=daily_lands(now=now),
    )