# SPDX-License-Identifier: Apache-2.0
"""The persisted conversation -- Console II spine.

Every operator turn and every zecor turn is one row in the `turn` table; the console's
centre pane *is* this log, so a reconnect over mosh or a fresh `zecor console` after a
reboot replays the same conversation because none of it lived in the TUI process.
`zecor watchd` writes turns with no TUI attached, so an event during a closed client is
still here when one reopens. SQLite WAL + a busy timeout, same as `core/leases.py`; the
integer primary key gives concurrent writers a monotonic, collision-free ordering.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from zecor import config

ROLES = ("you", "zecor")
KINDS = (
    "message",    # operator -> zecor, free text
    "reply",      # zecor -> operator, a Layer-2 answer
    "command",    # operator -> zecor, a resolved verb (audit trail)
    "run",        # a dispatch was kicked off / finished
    "narration",  # derived from the attempt table: what a lane did
    "question",   # zecor -> operator: a card needs you (see core/questions.py)
    "answer",     # operator -> zecor: the reply to a question
    "notice",     # zecor -> operator: a notification (zombie, silent run, ...)
    "error",      # something the console could not do
)

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS turn ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " ts TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,"
    " text TEXT NOT NULL, card_id TEXT, meta TEXT NOT NULL DEFAULT '{}')"
)
_INDEX = "CREATE INDEX IF NOT EXISTS turn_card ON turn (card_id)"


@dataclass(frozen=True)
class Turn:
    id: int
    ts: str
    role: str
    kind: str
    text: str
    card_id: str | None
    meta: dict[str, object]


def _db() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH, timeout=15.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 15000")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("PRAGMA journal_mode = WAL")
    db.execute(_SCHEMA)
    db.execute(_INDEX)
    db.commit()
    return db


def _row(r: sqlite3.Row) -> Turn:
    try:
        meta = json.loads(r["meta"] or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    return Turn(
        id=int(r["id"]), ts=str(r["ts"]), role=str(r["role"]), kind=str(r["kind"]),
        text=str(r["text"]), card_id=(r["card_id"] or None),
        meta=meta if isinstance(meta, dict) else {},
    )


def append(
    role: str,
    kind: str,
    text: str,
    *,
    card_id: str | None = None,
    meta: dict[str, object] | None = None,
    now: datetime | None = None,
) -> int:
    """Add one turn, return its id. `role`/`kind` are coerced to the known sets so a
    typo never makes an unrenderable row."""
    role = role if role in ROLES else "zecor"
    kind = kind if kind in KINDS else "notice"
    db = _db()
    try:
        cur = db.execute(
            "INSERT INTO turn (ts, role, kind, text, card_id, meta) VALUES (?,?,?,?,?,?)",
            (
                (now or datetime.now(UTC)).isoformat(),
                role, kind, text, card_id, json.dumps(meta or {}),
            ),
        )
        db.commit()
        return int(cur.lastrowid or 0)
    finally:
        db.close()


def tail(limit: int = 200) -> list[Turn]:
    """The most recent `limit` turns, oldest-first (render order)."""
    db = _db()
    try:
        rows = db.execute(
            "SELECT * FROM turn ORDER BY id DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
    finally:
        db.close()
    return [_row(r) for r in reversed(rows)]


def since(turn_id: int, limit: int = 1000) -> list[Turn]:
    """Turns after `turn_id`, oldest-first -- for an incremental UI repaint or watchd."""
    db = _db()
    try:
        rows = db.execute(
            "SELECT * FROM turn WHERE id > ? ORDER BY id ASC LIMIT ?",
            (turn_id, max(1, limit)),
        ).fetchall()
    finally:
        db.close()
    return [_row(r) for r in rows]


def for_card(card_id: str, limit: int = 100) -> list[Turn]:
    db = _db()
    try:
        rows = db.execute(
            "SELECT * FROM turn WHERE card_id = ? ORDER BY id ASC LIMIT ?",
            (card_id, max(1, limit)),
        ).fetchall()
    finally:
        db.close()
    return [_row(r) for r in rows]


def latest_id() -> int:
    db = _db()
    try:
        row = db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM turn").fetchone()
    finally:
        db.close()
    return int(row["m"])


def count() -> int:
    db = _db()
    try:
        return int(db.execute("SELECT COUNT(*) AS c FROM turn").fetchone()["c"])
    finally:
        db.close()


def prune(keep_days: int = 30) -> int:
    """Drop turns older than `keep_days`. Called on console mount so a long-lived
    transcript never slows the open. Returns rows deleted."""
    cutoff = (datetime.now(UTC) - timedelta(days=max(1, keep_days))).isoformat()
    db = _db()
    try:
        cur = db.execute("DELETE FROM turn WHERE ts < ?", (cutoff,))
        db.commit()
        return cur.rowcount
    finally:
        db.close()