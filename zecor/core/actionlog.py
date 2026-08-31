# SPDX-License-Identifier: Apache-2.0
"""Every mutating console verb, logged -- and a deliberately narrow `:undo`.

Zecor's mutations cross SQLite, card files, leases and the STOP file with no shared
transaction, so `undo_last()` only reverses `assign_move`, `stop`/`go`, and
`add_inbox`, each behind its own guard; everything else refuses but is still logged.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from zecor import config
from zecor.core.locks import atomic_write_text

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS action ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " ts TEXT NOT NULL, verb TEXT NOT NULL,"
    " args TEXT NOT NULL DEFAULT '{}', inverse TEXT,"  # inverse NULL => not reversible
    " undone_at TEXT)"
)

_REVERSIBLE = ("assign_move", "stop", "go", "add_inbox")


@dataclass(frozen=True)
class Action:
    id: int
    ts: str
    verb: str
    args: dict[str, object]
    reversible: bool
    undone: bool


@dataclass(frozen=True)
class UndoResult:
    ok: bool
    message: str


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


def record(
    verb: str,
    args: dict[str, object] | None = None,
    inverse: dict[str, object] | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """Log one mutating action. `inverse` is the payload `undo_last()` needs; pass None
    for a verb that cannot be safely reversed."""
    db = _db()
    try:
        cur = db.execute(
            "INSERT INTO action (ts, verb, args, inverse) VALUES (?,?,?,?)",
            (
                (now or datetime.now(UTC)).isoformat(),
                verb, json.dumps(args or {}),
                json.dumps(inverse) if inverse is not None else None,
            ),
        )
        db.commit()
        return int(cur.lastrowid or 0)
    finally:
        db.close()


def recent(limit: int = 50) -> list[Action]:
    db = _db()
    try:
        rows = db.execute(
            "SELECT * FROM action ORDER BY id DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
    finally:
        db.close()
    out: list[Action] = []
    for r in rows:
        try:
            args = json.loads(r["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        out.append(
            Action(
                int(r["id"]), str(r["ts"]), str(r["verb"]),
                args if isinstance(args, dict) else {},
                reversible=r["inverse"] is not None,
                undone=r["undone_at"] is not None,
            )
        )
    return out


def _mark_undone(action_id: int) -> None:
    db = _db()
    try:
        db.execute(
            "UPDATE action SET undone_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), action_id),
        )
        db.commit()
    finally:
        db.close()


def undo_last() -> UndoResult:
    """Reverse the most recent still-undone action, if it is in the safe set."""
    db = _db()
    try:
        row = db.execute(
            "SELECT id, verb, inverse FROM action WHERE undone_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()
    if row is None:
        return UndoResult(False, "nothing to undo")
    verb = str(row["verb"])
    if row["inverse"] is None or verb not in _REVERSIBLE:
        return UndoResult(False, f"'{verb}' cannot be undone")
    try:
        inv = json.loads(row["inverse"])
    except json.JSONDecodeError:
        return UndoResult(False, "undo record is corrupt")

    handler = {
        "assign_move": _undo_assign_move,
        "stop": _undo_stop_go,
        "go": _undo_stop_go,
        "add_inbox": _undo_add_inbox,
    }[verb]
    res = handler(inv)
    if res.ok:
        _mark_undone(int(row["id"]))
    return res


# -- inverse handlers ---------------------------------------------------------

def _undo_assign_move(inv: dict[str, object]) -> UndoResult:
    from zecor.core import cards as core_cards

    cid = str(inv.get("card_id", ""))
    prior = inv.get("prior")
    base_rev = inv.get("base_rev")
    prior_slug = str(prior) if prior else None

    def _set(c: object) -> None:
        c.move = prior_slug  # type: ignore[attr-defined]

    try:
        core_cards.mutate(
            cid, _set, base_rev=int(base_rev) if isinstance(base_rev, int) else None
        )
    except core_cards.CardNotFound:
        return UndoResult(False, f"card {cid} is gone")
    except core_cards.StaleRev:
        return UndoResult(False, f"{cid} changed since -- not undoing the move")
    return UndoResult(True, f"{cid} move restored to {prior_slug or '(unassigned)'}")


def _undo_stop_go(inv: dict[str, object]) -> UndoResult:
    # Manipulate the STOP file directly, NOT via intents.stop()/go() -- those log a
    # fresh reversible action, so a second `undo` would just toggle back and forth
    # instead of walking backward through history.
    import contextlib as _c

    left_stopped = bool(inv.get("resulting_stop"))
    if config.stop_requested() != left_stopped:
        return UndoResult(False, "STOP state already changed -- not undoing")
    if left_stopped:  # the action set STOP; undo => clear it
        with _c.suppress(FileNotFoundError):
            config.STOP_FILE.unlink()
        return UndoResult(True, "undo: STOP cleared")
    try:  # the action cleared STOP; undo => set it
        config.STOP_FILE.write_text("re-set by undo\n")
    except OSError as e:
        return UndoResult(False, f"could not set STOP: {e}")
    return UndoResult(True, "undo: STOP re-set")


def _undo_add_inbox(inv: dict[str, object]) -> UndoResult:
    line = str(inv.get("line", "")).rstrip()
    path = config.QUEUE_INBOX
    if not line or not path.exists():
        return UndoResult(False, "the inbox line is gone")
    from zecor.core.locks import file_lock

    with file_lock(path.parent / ".inbox.lock"):
        lines = path.read_text().splitlines()
        if line not in lines:
            return UndoResult(False, "the inbox line was already planned or removed")
        lines.remove(line)
        atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    return UndoResult(True, f"removed inbox line: {line}")