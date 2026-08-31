# SPDX-License-Identifier: Apache-2.0
"""Evaluate progress, update the roadmap -- Endurance step 4.

Once a day the supervisor calls `run_once()`: it rolls the roadmap up, compares it to
the last snapshot, appends a dated line to `progress/<iso-week>.md` for every Move that
landed/started/got stuck, marks a Move `stuck` after `STUCK_ATTEMPTS` attempts with
nothing landed (so `next_move()` skips it), and notifies on each stuck Move.
`evaluate()` is a pure function of two rollup snapshots, kept separate from the ledger
read and file write for testability. A `landed` event is confirmed against the
provenance JSONL when there is any, so a card marked `landed` whose branch never
actually merged isn't reported as done; with no provenance data yet it falls back to
card state.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from zecor import config
from zecor.core import roadmap
from zecor.core.view import CardView

STUCK_ATTEMPTS = 6  # attempts across a Move's cards with 0 lands => stuck

_KIND = ("landed", "started", "stuck")


@dataclass(frozen=True)
class ProgressEvent:
    kind: str  # landed | started | stuck
    slug: str
    title: str
    detail: str = ""


@dataclass(frozen=True)
class ProgressReport:
    events: list[ProgressEvent]
    log_path: Path | None


def _moves(rolled: list[roadmap.Rolled]) -> dict[str, roadmap.Rolled]:
    out: dict[str, roadmap.Rolled] = {}

    def walk(r: roadmap.Rolled) -> None:
        if r.kind == "move":
            out[r.slug] = r
        for c in r.children:
            walk(c)

    for r in rolled:
        walk(r)
    return out


def evaluate(
    before: list[roadmap.Rolled],
    after: list[roadmap.Rolled],
    *,
    stuck_before: set[str],
    stuck_after: set[str],
) -> list[ProgressEvent]:
    """Move-level deltas between two rollups. Pure."""
    b, a = _moves(before), _moves(after)
    events: list[ProgressEvent] = []
    for slug, ra in a.items():
        rb = b.get(slug)
        was = rb.status if rb else "unplanned"
        if ra.status == "landed" and was != "landed":
            events.append(ProgressEvent("landed", slug, ra.title, f"{ra.landed}/{ra.total} cards"))
        elif was == "unplanned" and ra.status in ("ready", "in-progress"):
            events.append(ProgressEvent("started", slug, ra.title, f"{ra.total} card(s) planned"))
    for slug in sorted(stuck_after - stuck_before):
        node = a.get(slug)
        events.append(ProgressEvent("stuck", slug, node.title if node else slug))
    return events


def detect_stuck(
    missions: list[roadmap.Node],
    cards: list[CardView],
    *,
    k: int = STUCK_ATTEMPTS,
) -> list[str]:
    """Move slugs whose cards have >= k attempts and 0 lands -- work that is going
    nowhere. Read from the attempt ledger."""
    from zecor.core import view

    by_move: dict[str, list[str]] = {}
    for c in cards:
        if c.move:
            by_move.setdefault(c.move, []).append(c.id)
    known_moves = {
        n.slug for m in missions for c in m.children for n in c.children if n.kind == "move"
    }
    out: list[str] = []
    for slug, ids in by_move.items():
        if slug not in known_moves:
            continue
        attempts = lands = 0
        for cid in ids:
            for a in view.attempts(cid, limit=100):
                attempts += 1
                if a.outcome == "pass":
                    lands += 1
        if attempts >= k and lands == 0:
            out.append(slug)
    return out


def _provenance_has_data() -> bool:
    root = config.ZECOR_ROOT / "provenance"
    try:
        return any(f.stat().st_size > 0 for f in root.glob("*.jsonl"))
    except OSError:
        return False


def _iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _write_log(events: list[ProgressEvent], *, now: datetime) -> Path:
    root = config.ZECOR_ROOT / "progress"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_iso_week(now)}.md"
    header = f"# Progress {_iso_week(now)}\n\n"
    existing = path.read_text() if path.exists() else header
    if not existing.startswith("#"):
        existing = header + existing
    day = now.date().isoformat()
    lines = [f"- {day}  {e.kind:8}  {e.slug:22}  {e.title}"
             + (f"  -- {e.detail}" if e.detail else "")
             for e in events]
    body = existing.rstrip() + "\n" + "\n".join(lines) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


_SNAP = "progress/.last.json"


def _load_snapshot() -> tuple[dict[str, str], set[str]]:
    p = config.ZECOR_ROOT / _SNAP
    try:
        obj = json.loads(p.read_text())
        return dict(obj.get("moves", {})), set(obj.get("stuck", []))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}, set()


def _save_snapshot(moves: dict[str, str], stuck: set[str]) -> None:
    p = config.ZECOR_ROOT / _SNAP
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".last.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"moves": moves, "stuck": sorted(stuck)}, fh)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _rolled_as_before(moves: dict[str, str]) -> list[roadmap.Rolled]:
    """Rebuild a minimal rollup (just Move slug+status) from a saved snapshot."""
    return [
        roadmap.Rolled(kind="move", slug=s, title=s, status=st, landed=0, total=0,
                       oldest_mtime=0.0)
        for s, st in moves.items()
    ]


def run_once(*, k: int = STUCK_ATTEMPTS, now: datetime | None = None) -> ProgressReport:
    from zecor.core import replenish, view

    ts = now or datetime.now(UTC)
    missions = roadmap.load()
    cards = view.cards()
    after = roadmap.rollup(missions, cards)
    after_moves = {s: r.status for s, r in _moves(after).items()}

    prev_moves, prev_stuck = _load_snapshot()
    newly_stuck = [s for s in detect_stuck(missions, cards, k=k) if s not in prev_stuck]
    for slug in newly_stuck:
        with contextlib.suppress(Exception):
            replenish.mark_stuck(slug, f"{k}+ attempts, 0 lands")
    stuck_after = replenish.stuck_moves()

    events = evaluate(
        _rolled_as_before(prev_moves), after,
        stuck_before=prev_stuck, stuck_after=stuck_after,
    )
    if events and _provenance_has_data():
        events = [e for e in events if e.kind != "landed" or _landed_confirmed(e.slug, cards)]

    log_path = _write_log(events, now=ts) if events else None
    for e in events:
        if e.kind == "stuck":
            with contextlib.suppress(Exception):
                from zecor import notify

                notify.notify(f"Move stuck: {e.slug}", e.title, level="warn")
    _save_snapshot(after_moves, stuck_after)
    return ProgressReport(events, log_path)


def _landed_confirmed(slug: str, cards: list[CardView]) -> bool:
    ids = [c.id for c in cards if c.move == slug]
    if not ids:
        return False
    root = config.ZECOR_ROOT / "provenance"
    try:
        blob = "\n".join(f.read_text() for f in root.glob("*.jsonl"))
    except OSError:
        return True  # cannot check -> trust card state
    return all(cid in blob for cid in ids)