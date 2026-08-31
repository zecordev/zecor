# SPDX-License-Identifier: Apache-2.0
"""Pure presentation logic for the console -- no Textual, no I/O.

`app.py` is a thin Textual shell over these functions and `core.intents`; keeping the
formatting and the change-detection here means it is all unit-testable without a
terminal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from zecor.core.view import CardView, Health

# Worst-first: the order the operator should scan; anything unknown sorts last.
STATE_ORDER = (
    "gate-broken",
    "blocked",
    "needs-human-design",
    "deferred",
    "unreadable",
    "ready",
    "landed",
)

COLUMNS = ("id", "state", "repo", "klass", "intent", "try", "age")


def state_rank(state: str) -> int:
    try:
        return STATE_ORDER.index(state)
    except ValueError:
        return len(STATE_ORDER)


def age(mtime: float, *, now: float | None = None) -> str:
    """Compact age of the last card write: 12s / 4m / 3h / 2d / -."""
    if not mtime:
        return "-"
    secs = max(0.0, (now if now is not None else time.time()) - mtime)
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 90 * 60:
        return f"{secs / 60:.0f}m"
    if secs < 36 * 3600:
        return f"{secs / 3600:.0f}h"
    return f"{secs / 86400:.0f}d"


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass(frozen=True)
class Row:
    key: str  # the card id -- DataTable row key, stable across refreshes
    cells: tuple[str, ...]
    zombie: bool
    state: str


def rows(cards: list[CardView], *, now: float | None = None, intent_width: int = 60) -> list[Row]:
    """Table rows, worst state first then by id."""
    ordered = sorted(cards, key=lambda c: (state_rank(c.state), c.id))
    out: list[Row] = []
    for c in ordered:
        state = "ZOMBIE" if c.zombie else c.state
        out.append(
            Row(
                key=c.id,
                cells=(
                    c.id,
                    state,
                    _clip(c.repo, 18),
                    c.klass,
                    _clip(c.intent, intent_width),
                    str(c.retry_count or ""),
                    age(c.mtime, now=now),
                ),
                zombie=c.zombie,
                state=c.state,
            )
        )
    return out


def snapshot_key(cards: list[CardView], *, now: float | None = None) -> tuple[object, ...]:
    """Cheap value that changes iff the rendered table would differ -- so a 1s poll that
    finds nothing new does no work and never disturbs the cursor. Includes each card's
    `age()` label so a quiet board still ticks 12s -> 1m -> 2h without an edit."""
    return tuple(
        (c.id, c.state, c.rev, c.zombie, c.retry_count, age(c.mtime, now=now))
        for c in cards
    )


def status_line(
    h: Health,
    *,
    nlu_ok: bool = True,
    repo_focus: str | None = None,
    read_only: bool = False,
    momentum: str = "",
) -> str:
    flag = "[b red]STOP[/]" if h.stopped else "[b green]GO[/]"
    disk = f"disk {h.disk_free_gb}G/{h.disk_floor_gb}G"
    lanes = f"lanes {h.advised_lanes}/{h.max_lanes}"
    counts = " ".join(f"{k}={v}" for k, v in sorted(h.counts.items())) or "no cards"
    parts = [flag, counts, lanes, disk]
    if momentum:
        parts.append(f"[green]{momentum}[/]")
    if not nlu_ok:
        parts.append("[yellow]NLU:off[/]")
    if repo_focus:
        parts.append(f"[reverse] {repo_focus} [/]")
    if read_only:
        parts.append("[b yellow]READ-ONLY[/]")
    if h.resource_note:
        parts.append(f"[dim]{h.resource_note}[/]")
    return "  ".join(parts)


def momentum(landed_total: int, landed_today: int, spark: str) -> str:
    """A small 'is it moving' cue for the status bar: cumulative lands, today's, and a
    14-day sparkline. Empty until anything has landed."""
    if not landed_total:
        return ""
    bits = [f"✦{landed_total}"]
    if landed_today:
        bits.append(f"↑{landed_today} today")
    if spark and set(spark) != {"▁"}:
        bits.append(spark)
    return " ".join(bits)


# responsive layout: pick a pane arrangement from the terminal width.
LAYOUTS = ("narrow", "medium", "wide")


def layout_for(width: int) -> str:
    """<100 cols: chat only (phone over mosh). 100-140: chat + board rail.
    140+: chat + roadmap + board + output pane."""
    if width < 100:
        return "narrow"
    if width < 140:
        return "medium"
    return "wide"


def turn_line(role: str, kind: str, text: str) -> str:
    """One transcript row, Rich-markup, for the RichLog pane."""
    who = {
        "you": "[b cyan]you[/]",
        "zecor": "[b #e2a458]zecor[/]",
    }.get(role, role)
    tag = {
        "question": "[b yellow]?[/] ",
        "answer": "[cyan]>[/] ",
        "narration": "[dim]·[/] ",
        "notice": "[yellow]![/] ",
        "error": "[red]![/] ",
        "command": "[dim]$[/] ",
        "run": "[dim]»[/] ",
    }.get(kind, "")
    first = text.split("\n", 1)[0]
    rest = text.split("\n", 1)[1] if "\n" in text else ""
    line = f"{who}  {tag}{first}"
    return line + (f"\n      {rest}" if rest else "")