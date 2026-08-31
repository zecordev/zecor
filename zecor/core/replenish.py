# SPDX-License-Identifier: Apache-2.0
"""Feed the queue from the roadmap -- Endurance step 1.

`zecor run` drains the queue then exits, so `replenish_once()` closes the loop: pick
the next Move that needs work, decompose it, and stamp the new cards with `move:`
frontmatter -- bypassing the human ratify-by-hand step in `to_inbox_block`. The
decompose/create/bookkeeping sequence is tracked in a `replenish_op` row so a crash
mid-run is rolled back next tick instead of planting duplicate work; a Move decompose
can't turn into real work is marked `stuck` and skipped so the roadmap keeps advancing.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from zecor import config
from zecor.core import cards as core_cards
from zecor.core import roadmap
from zecor.core.roadmap import Node
from zecor.core.view import CardView

if TYPE_CHECKING:
    from zecor.decompose import Decomposition

LOW_WATER = 2  # provision a Move once it has fewer than this many live cards
BATCH = 3  # cards to add per replenish tick -- keep the queue shallow, like a human would
STALE_OP_S = 1800  # a `running` op older than this is presumed crashed and rolled back

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS replenish_op ("
    " id INTEGER PRIMARY KEY, move_slug TEXT NOT NULL,"
    " started_at TEXT NOT NULL, completed_at TEXT,"
    " status TEXT NOT NULL,"  # running | done | stuck | rolled_back
    " created_ids TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '')"
)

_TERMINAL = ("landed",)


class ReplenishError(RuntimeError):
    """A transient failure -- the frontier was down, a repo would not resolve. The op is
    rolled back, nothing is marked stuck, and the caller retries next tick."""


@dataclass(frozen=True)
class ReplenishResult:
    acted: bool
    move: str | None = None
    created: list[str] = field(default_factory=list)
    reason: str = ""
    stuck: bool = False


def _db() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH, timeout=15.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 15000")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("PRAGMA journal_mode = WAL")
    db.execute(_SCHEMA)
    db.commit()
    return db


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


# -- stuck-Move bookkeeping --------------------------------------------------

def stuck_moves() -> set[str]:
    db = _db()
    try:
        rows = db.execute(
            "SELECT DISTINCT move_slug FROM replenish_op WHERE status = 'stuck'"
        ).fetchall()
    finally:
        db.close()
    return {r["move_slug"] for r in rows}


def mark_stuck(slug: str, note: str) -> None:
    db = _db()
    try:
        db.execute(
            "INSERT INTO replenish_op (move_slug, started_at, status, note) "
            "VALUES (?,?,?,?)",
            (slug, _now(None).isoformat(), "stuck", note),
        )
        db.commit()
    finally:
        db.close()


def clear_stuck(slug: str) -> int:
    """Un-stick a Move a human has since made workable. Returns rows cleared."""
    db = _db()
    try:
        cur = db.execute(
            "UPDATE replenish_op SET status = 'rolled_back', "
            "note = note || ' (unstuck)' WHERE move_slug = ? AND status = 'stuck'",
            (slug,),
        )
        db.commit()
        return cur.rowcount
    finally:
        db.close()


# -- pure selection -------------------------------------------------------------

def _active_cards(slug: str, cards: list[CardView]) -> int:
    return sum(
        1 for c in cards
        if c.move == slug and c.state not in _TERMINAL and c.state != "unreadable"
    )


def next_move(
    missions: list[Node],
    cards: list[CardView],
    *,
    stuck: set[str],
    low_water: int = LOW_WATER,
) -> Node | None:
    """The next Move to provision, or None if nothing needs work.

    Walks Campaigns then Moves in document order; the first Move that is not
    landed/stuck is the frontier -- returned if unplanned or thin (< `low_water`
    cards), else we stop rather than jump ahead of unlanded work.
    """
    for mission in roadmap.rollup(missions, cards):
        for campaign in mission.children:
            for mv in campaign.children:
                if mv.slug in stuck or mv.status == "landed":
                    continue
                node = _find(missions, mv.slug)
                if node is None:
                    continue
                if mv.status == "unplanned":
                    return node
                if mv.status in ("ready", "in-progress"):
                    if _active_cards(mv.slug, cards) < low_water:
                        return node
                    return None  # being worked and adequately stocked -- don't pile on
                return None  # blocked / needs-human / gate-broken -- wait, don't skip
    return None


def _find(missions: list[Node], slug: str) -> Node | None:
    stack = list(missions)
    while stack:
        n = stack.pop()
        if n.kind == "move" and n.slug == slug:
            return n
        stack.extend(n.children)
    return None


def _context_doc(missions: list[Node], slug: str) -> str:
    """The Move's own prose plus its Campaign and Mission headings -- the requirements
    text handed to `decompose`."""
    for mission in missions:
        for campaign in mission.children:
            for mv in campaign.children:
                if mv.slug == slug:
                    return (
                        f"{mission.title}\n{mission.body}\n\n"
                        f"## {campaign.title}\n{campaign.body}\n\n"
                        f"### {mv.title}\n{mv.body}\n"
                    ).strip()
    return ""


# -- the transaction ----------------------------------------------------------

def _rollback_stale(now: datetime) -> None:
    """A `running` op older than STALE_OP_S is a crash. Delete the pristine cards it
    made (still `ready`, never retried) and mark it rolled_back."""
    db = _db()
    try:
        rows = db.execute(
            "SELECT id, created_ids, started_at FROM replenish_op WHERE status = 'running'"
        ).fetchall()
        for r in rows:
            try:
                started = datetime.fromisoformat(r["started_at"])
            except ValueError:
                started = now
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if (now - started).total_seconds() < STALE_OP_S:
                continue
            for cid in json.loads(r["created_ids"] or "[]"):
                _delete_pristine_card(cid)
            db.execute(
                "UPDATE replenish_op SET status = 'rolled_back', completed_at = ?, "
                "note = note || ' (stale-rollback)' WHERE id = ?",
                (now.isoformat(), r["id"]),
            )
        db.commit()
    finally:
        db.close()


def _delete_pristine_card(card_id: str) -> None:
    """Remove a card this module made only if nothing has touched it -- still `ready`,
    never retried. A card a lane has since picked up is left alone."""
    try:
        card = core_cards.read(card_id)
    except core_cards.CardNotFound:
        return
    if card.state == "ready" and card.retry_count == 0:
        with contextlib.suppress(OSError):
            (config.TASKS_DIR / f"{card_id}.md").unlink()


def _op_start(slug: str, now: datetime) -> int:
    """Begin a replenish op. Returns 0 if another op is already `running` and not
    stale -- one replenish at a time, so two supervisors can't both provision the
    roadmap."""
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT started_at FROM replenish_op WHERE status = 'running'"
        ).fetchall()
        for r in rows:
            try:
                started = datetime.fromisoformat(r["started_at"])
            except ValueError:
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if (now - started).total_seconds() < STALE_OP_S:
                db.rollback()
                return 0
        cur = db.execute(
            "INSERT INTO replenish_op (move_slug, started_at, status) VALUES (?,?, 'running')",
            (slug, now.isoformat()),
        )
        db.commit()
        return int(cur.lastrowid or 0)
    finally:
        db.close()


def _op_record_card(op_id: int, card_id: str) -> None:
    db = _db()
    try:
        row = db.execute(
            "SELECT created_ids FROM replenish_op WHERE id = ?", (op_id,)
        ).fetchone()
        ids = json.loads(row["created_ids"] or "[]") if row else []
        ids.append(card_id)
        db.execute(
            "UPDATE replenish_op SET created_ids = ? WHERE id = ?",
            (json.dumps(ids), op_id),
        )
        db.commit()
    finally:
        db.close()


def _op_finish(op_id: int, status: str, now: datetime, note: str = "") -> None:
    db = _db()
    try:
        db.execute(
            "UPDATE replenish_op SET status = ?, completed_at = ?, "
            "note = CASE WHEN ? = '' THEN note ELSE ? END WHERE id = ?",
            (status, now.isoformat(), note, note, op_id),
        )
        db.commit()
    finally:
        db.close()


def _known_repos() -> set[str]:
    try:
        from zecor.repos import repo_roots

        return {p.name for p in repo_roots() if p.is_dir()}
    except Exception:
        return set()


def replenish_once(
    *,
    low_water: int = LOW_WATER,
    batch: int = BATCH,
    now: datetime | None = None,
    decompose_fn: Callable[[str], Decomposition] | None = None,
) -> ReplenishResult:
    """One tick: roll back a crashed op, pick the frontier Move, decompose it, plan the
    cards with `move:` set. `decompose_fn` is injectable for tests."""
    from zecor.cards import InboxItem, parse_inbox
    from zecor.decompose import decompose as _decompose
    from zecor.planner import plan_item

    dc: Callable[[str], Decomposition] = decompose_fn or _decompose
    ts = _now(now)
    _rollback_stale(ts)

    missions = roadmap.load()
    if not missions:
        return ReplenishResult(False, reason="no roadmap")
    from zecor.core import view

    cards = view.cards()
    mv = next_move(missions, cards, stuck=stuck_moves(), low_water=low_water)
    if mv is None:
        return ReplenishResult(False, reason="nothing needs provisioning")

    op_id = _op_start(mv.slug, ts)
    if op_id == 0:
        return ReplenishResult(False, mv.slug, reason="another replenish is running")
    doc = _context_doc(missions, mv.slug)
    try:
        d = dc(doc)
    except Exception as e:  # NoFrontierAvailable and friends -- transient, not stuck
        _op_finish(op_id, "rolled_back", ts, f"decompose failed: {e}")
        raise ReplenishError(str(e)) from e

    repo = (d.repo or "").strip()
    known = _known_repos()
    if not repo or (known and repo not in known):
        # the op row itself carries status='stuck' -- no separate mark_stuck needed
        _op_finish(op_id, "stuck", ts, f"decompose named no known repo ({repo!r})")
        return ReplenishResult(False, mv.slug, reason="unknown target repo", stuck=True)

    ready_tasks = [
        t for t in d.tasks
        if bool(t.get("ready", True)) and str(t.get("intent", "")).strip()
    ]
    lines = [f"- {repo}: {str(t['intent']).strip()}" for t in ready_tasks[:batch]]
    items = parse_inbox("\n".join(lines))
    if not items:
        _op_finish(op_id, "stuck", ts, "decompose produced no landable tasks")
        return ReplenishResult(False, mv.slug, reason="no usable tasks", stuck=True)

    created: list[str] = []
    base = core_cards.next_id()
    try:
        for i, item in enumerate(items):
            cid = f"{base + i:04d}"
            card = plan_item(InboxItem(item.repo, item.intent, item.raw), cid)
            card.move = mv.slug
            _op_record_card(op_id, cid)  # record BEFORE create -- a crash here leaves a
            try:                          # tracked id the stale-rollback can still clean
                core_cards.create(card)
            except core_cards.CardExists:
                continue
            created.append(cid)
    except Exception as e:  # planner / disk / anything -- don't leave the op running
        for done in created:
            _delete_pristine_card(done)
        _op_finish(op_id, "rolled_back", ts, f"planning failed: {e}")
        raise ReplenishError(str(e)) from e

    if not created:
        _op_finish(op_id, "rolled_back", ts, "all ids collided")
        raise ReplenishError("id collision -- retry next tick")

    _op_finish(op_id, "done", ts, f"planned {len(created)} under {mv.slug}")
    return ReplenishResult(True, mv.slug, created, reason=f"planned {len(created)}")