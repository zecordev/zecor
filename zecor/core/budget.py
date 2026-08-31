# SPDX-License-Identifier: Apache-2.0
"""The unattended spend ceiling -- Endurance.

`ceiling_state()` folds two signals -- the `quota` table's vendor-reported usage, and a
rough USD estimate from the `attempt` ledger vs `ZECOR_SPEND_DAILY_USD` /
`ZECOR_SPEND_WEEKLY_USD` -- into one of three states: `ok` (run normally), `throttle`
at 80% of any limit (`route()` drops the frontier rungs, local-only until a window
resets), or `pause` at 100% (the supervisor sets STOP and notifies; nothing
dispatches).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from zecor.core import metrics

THROTTLE_FRAC = 0.8
STATES = ("ok", "throttle", "pause")


@dataclass(frozen=True)
class CeilingState:
    state: str  # ok | throttle | pause
    reason: str

    @property
    def may_spend_frontier(self) -> bool:
        return self.state == "ok"

    @property
    def should_pause(self) -> bool:
        return self.state == "pause"


def _limit(name: str) -> float:
    try:
        v = float(os.environ.get(name, "0") or "0")
    except ValueError:
        return 0.0
    return v if v > 0 else 0.0


def _quota_state() -> tuple[str, str]:
    try:
        from zecor.core import view

        rows = view.quota()
    except Exception:
        return "ok", ""
    worst = "ok"
    reason = ""
    for r in rows:
        if r.cap is None or r.cap <= 0:
            continue
        frac = r.used / r.cap
        if frac >= 1.0:
            return "pause", f"{r.vendor} {r.window} quota exhausted ({r.used}/{r.cap})"
        if frac >= THROTTLE_FRAC and worst == "ok":
            worst = "throttle"
            reason = f"{r.vendor} {r.window} quota at {frac:.0%}"
    return worst, reason


def _usd_state(*, now: datetime | None = None) -> tuple[str, str]:
    ts = now or datetime.now(UTC)
    daily = _limit("ZECOR_SPEND_DAILY_USD")
    weekly = _limit("ZECOR_SPEND_WEEKLY_USD")
    worst, reason = "ok", ""
    checks = []
    if daily:
        spent = metrics.est_cost_usd(since=(ts - timedelta(days=1)).isoformat())
        checks.append(("daily", spent, daily))
    if weekly:
        spent = metrics.est_cost_usd(since=(ts - timedelta(days=7)).isoformat())
        checks.append(("weekly", spent, weekly))
    for window, spent, limit in checks:
        frac = spent / limit if limit else 0.0
        if frac >= 1.0:
            return "pause", f"{window} spend ${spent:.2f} >= ${limit:.0f} ceiling"
        if frac >= THROTTLE_FRAC and worst == "ok":
            worst, reason = "throttle", f"{window} spend ${spent:.2f} of ${limit:.0f}"
    return worst, reason


def ceiling_state(*, now: datetime | None = None) -> CeilingState:
    qs, qr = _quota_state()
    us, ur = _usd_state(now=now)
    order = {s: i for i, s in enumerate(STATES)}
    if order[qs] >= order[us]:
        state, reason = qs, qr
    else:
        state, reason = us, ur
    return CeilingState(state, reason or ("within budget" if state == "ok" else state))