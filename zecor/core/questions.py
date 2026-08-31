# SPDX-License-Identifier: Apache-2.0
"""NEEDS_HUMAN as a durable message -- Console II.

When a card lands in a human-only state, Zecor posts a question: one row here plus a
`question` transcript turn; the operator's plain-text answer becomes the retry note.
Posting is deduped by `(card_id, reason_hash)` so a restarting `scan()` never reposts
the same reason, and `answer()` marks the row answered before retrying (reopening it
on failure) so concurrent answers can't double-dispatch.
"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from zecor import config

# card states that mean "a human has to decide something"
ASKABLE = ("needs-human-design", "blocked", "gate-broken")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS question ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " card_id TEXT NOT NULL, reason_hash TEXT NOT NULL, reason TEXT NOT NULL,"
    " created_at TEXT NOT NULL, answered_at TEXT, answer TEXT)"
)
_OPEN_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS question_open "
    "ON question (card_id, reason_hash) WHERE answered_at IS NULL"
)


@dataclass(frozen=True)
class Question:
    id: int
    card_id: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class AnswerResult:
    ok: bool
    message: str
    card_id: str | None = None


def _db() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH, timeout=15.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 15000")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("PRAGMA journal_mode = WAL")
    db.execute(_SCHEMA)
    db.execute(_OPEN_UNIQUE)
    db.commit()
    return db


def _hash(reason: str) -> str:
    raw = reason.strip().encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:12]


# -- a minimal card shape so this module does not depend on the full view --------

class _Cardish(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def state(self) -> str: ...


ReasonFn = Callable[[str], str]


def _default_reason(card_id: str) -> str:
    """Best-effort blocking reason: the last attempt's `reason`, else a placeholder."""
    with contextlib.suppress(Exception):
        from zecor.core import view

        rows = view.attempts(card_id, limit=1)
        if rows and rows[0].reason:
            return rows[0].reason
    return "no reason recorded"


def scan(
    cards: Sequence[_Cardish],
    *,
    reason_fn: ReasonFn | None = None,
    now: datetime | None = None,
) -> list[int]:
    """Post a question for every askable card that does not already have an open one for
    its current reason. Returns the ids created. Safe to call on every poll."""
    from zecor.core import transcript

    rf = reason_fn or _default_reason
    ts = (now or datetime.now(UTC)).isoformat()
    made: list[int] = []
    db = _db()
    try:
        for c in cards:
            if c.state not in ASKABLE:
                continue
            reason = rf(c.id).strip() or "no reason recorded"
            rh = _hash(reason)
            exists = db.execute(
                "SELECT 1 FROM question WHERE card_id = ? AND reason_hash = ? "
                "AND answered_at IS NULL",
                (c.id, rh),
            ).fetchone()
            if exists:
                continue
            try:
                cur = db.execute(
                    "INSERT INTO question (card_id, reason_hash, reason, created_at) "
                    "VALUES (?,?,?,?)",
                    (c.id, rh, reason, ts),
                )
                db.commit()
            except sqlite3.IntegrityError:
                continue  # a concurrent scan just made it
            qid = int(cur.lastrowid or 0)
            made.append(qid)
            with contextlib.suppress(Exception):
                transcript.append(
                    "zecor", "question",
                    f"{c.id} needs you ({c.state}): {reason}",
                    card_id=c.id, meta={"question_id": qid},
                )
    finally:
        db.close()
    return made


def open_questions() -> list[Question]:
    db = _db()
    try:
        rows = db.execute(
            "SELECT id, card_id, reason, created_at FROM question "
            "WHERE answered_at IS NULL ORDER BY id ASC"
        ).fetchall()
    finally:
        db.close()
    return [Question(int(r["id"]), r["card_id"], r["reason"], r["created_at"]) for r in rows]


def open_for_card(card_id: str) -> list[Question]:
    return [q for q in open_questions() if q.card_id == card_id]


def _close(db: sqlite3.Connection, qid: int, text: str, ts: str) -> bool:
    cur = db.execute(
        "UPDATE question SET answered_at = ?, answer = ? WHERE id = ? AND answered_at IS NULL",
        (ts, text, qid),
    )
    db.commit()
    return cur.rowcount > 0


def _reopen(qid: int) -> None:
    """Put a question back open after a failed retry, unless another open row already
    covers the same (card_id, reason_hash) -- a `scan()` in the retry window may have
    re-posted it, and reopening this one too would violate the partial unique index."""
    db = _db()
    try:
        db.execute(
            "UPDATE question SET answered_at = NULL, answer = NULL "
            "WHERE id = :qid AND NOT EXISTS ("
            "  SELECT 1 FROM question o WHERE o.card_id = ("
            "    SELECT card_id FROM question WHERE id = :qid) "
            "  AND o.reason_hash = (SELECT reason_hash FROM question WHERE id = :qid) "
            "  AND o.answered_at IS NULL)",
            {"qid": qid},
        )
        db.commit()
    finally:
        db.close()


def answer(qid: int, text: str, *, now: datetime | None = None) -> AnswerResult:
    """Mark the question answered (guarded), record an `answer` turn, then retry the
    card with the answer as its note. Reopen the question if the retry fails."""
    from zecor.core import transcript
    from zecor.core.intents import retry as _retry

    text = " ".join(text.split())
    if not text:
        return AnswerResult(False, "an answer cannot be empty")
    ts = (now or datetime.now(UTC)).isoformat()

    db = _db()
    try:
        row = db.execute(
            "SELECT card_id FROM question WHERE id = ? AND answered_at IS NULL", (qid,)
        ).fetchone()
        if row is None:
            return AnswerResult(False, f"question {qid} is not open")
        card_id = str(row["card_id"])
        if not _close(db, qid, text, ts):
            return AnswerResult(False, f"question {qid} was just answered elsewhere")
    finally:
        db.close()

    with contextlib.suppress(Exception):
        transcript.append("you", "answer", text, card_id=card_id, meta={"question_id": qid})

    res = _retry(card_id, note=text)
    if not res.ok:
        _reopen(qid)
        return AnswerResult(False, f"retry refused: {res.message}", card_id)
    return AnswerResult(True, res.message, card_id)


def answer_for_card(card_id: str, text: str, *, now: datetime | None = None) -> AnswerResult:
    """Answer the one open question for a card. Ambiguous if there are none or several
    -- the console then names the question id explicitly."""
    ops = open_for_card(card_id)
    if not ops:
        return AnswerResult(False, f"{card_id} has no open question")
    if len(ops) > 1:
        ids = ", ".join(f"#{x.id}" for x in ops)
        return AnswerResult(
            False, f"{card_id} has {len(ops)} open questions ({ids}) -- answer by id"
        )
    return answer(ops[0].id, text, now=now)