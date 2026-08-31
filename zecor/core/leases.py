# SPDX-License-Identifier: Apache-2.0
"""Dispatch leases in SQLite -- Command Center, per the agreed design.

A lease says "a lane is working this card right now"; being high-frequency volatile
state (a 60s heartbeat), it lives in its own `lease` table, never in the card file, so
the card's mtime and the `attempt` quota counts stay meaningful. `token` -- a per-claim
random id -- is the ownership key, not the pid, since two threads share a pid and pids
get reused; `claim()` reaps a row whose pid is dead. A suspend freezes the holder
without killing it, so `is_zombie()` gives a live holder `ZOMBIE_GRACE_S` of slack
before calling it dead; the woken lane's next `refresh()` still raises `LeaseLost` if
another lane reaped-and-stole it in the meantime.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from zecor import config

LEASE_REFRESH_S = 60
DEFAULT_LEASE_TTL_S = 300  # 5x the refresh cadence
ZOMBIE_GRACE_S = 600  # a live holder past expiry by this long (a long suspend) is a zombie

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS lease ("
    " task_id TEXT PRIMARY KEY, token TEXT NOT NULL, pid INTEGER,"
    " expires TEXT NOT NULL, updated_at TEXT NOT NULL)"
)


class Leased(Exception):
    """claim() found a live lease held by another token."""


class LeaseLost(Exception):
    """refresh() found the lease is no longer ours -- reaped-and-stolen, or cleared."""


@dataclass(frozen=True)
class LeaseRow:
    task_id: str
    token: str
    pid: int | None
    expires: str


def _now() -> datetime:
    return datetime.now(UTC)


def _iso_in(secs: float) -> str:
    return (_now() + timedelta(seconds=secs)).isoformat()


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


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


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _row_live(r: LeaseRow) -> bool:
    """Still worth respecting for claim(): holder alive AND within expiry + grace."""
    if not _pid_alive(r.pid):
        return False
    exp = _parse(r.expires)
    return exp is not None and _now() < exp + timedelta(seconds=ZOMBIE_GRACE_S)


def new_token() -> str:
    return secrets.token_hex(8)


def get(task_id: str) -> LeaseRow | None:
    db = _db()
    try:
        row = db.execute(
            "SELECT task_id, token, pid, expires FROM lease WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        db.close()
    if row is None:
        return None
    return LeaseRow(row["task_id"], row["token"], row["pid"], row["expires"])


def active() -> dict[str, LeaseRow]:
    """Every lease row, keyed by task id -- one query for the whole board."""
    db = _db()
    try:
        rows = db.execute("SELECT task_id, token, pid, expires FROM lease").fetchall()
    finally:
        db.close()
    return {r["task_id"]: LeaseRow(r["task_id"], r["token"], r["pid"], r["expires"]) for r in rows}


def claim(task_id: str, token: str, *, ttl_s: int = DEFAULT_LEASE_TTL_S) -> None:
    """Take the lease under `token`. Reaps a dead/expired lease; raises `Leased` if a
    live lease held by a different token is present."""
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT task_id, token, pid, expires FROM lease WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is not None:
            existing = LeaseRow(row["task_id"], row["token"], row["pid"], row["expires"])
            if existing.token != token and _row_live(existing):
                db.rollback()
                raise Leased(f"{task_id}: held by pid {existing.pid}")
        db.execute(
            "INSERT INTO lease (task_id, token, pid, expires, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET token=excluded.token, pid=excluded.pid, "
            "expires=excluded.expires, updated_at=excluded.updated_at",
            (task_id, token, os.getpid(), _iso_in(ttl_s), _now().isoformat()),
        )
        db.commit()
    finally:
        db.close()


def refresh(task_id: str, token: str, *, ttl_s: int = DEFAULT_LEASE_TTL_S) -> None:
    db = _db()
    try:
        cur = db.execute(
            "UPDATE lease SET expires = ?, updated_at = ?, pid = ? WHERE task_id = ? AND token = ?",
            (_iso_in(ttl_s), _now().isoformat(), os.getpid(), task_id, token),
        )
        db.commit()
        if cur.rowcount == 0:
            raise LeaseLost(f"{task_id}: lease is no longer ours")
    finally:
        db.close()


def release(task_id: str, token: str) -> None:
    db = _db()
    try:
        db.execute("DELETE FROM lease WHERE task_id = ? AND token = ?", (task_id, token))
        db.commit()
    finally:
        db.close()


def clear(task_id: str) -> None:
    """Operator's zombie kill key: drop the lease whoever holds it. The holder's next
    refresh() then raises LeaseLost and it stops."""
    db = _db()
    try:
        db.execute("DELETE FROM lease WHERE task_id = ?", (task_id,))
        db.commit()
    finally:
        db.close()


def clear_if(task_id: str, token: str) -> bool:
    """Drop the lease only if it still carries `token` -- so a caller reaping a zombie it
    observed cannot delete a fresh lease another lane claimed in between. Returns whether
    a row was removed."""
    db = _db()
    try:
        cur = db.execute(
            "DELETE FROM lease WHERE task_id = ? AND token = ?", (task_id, token)
        )
        db.commit()
        return cur.rowcount > 0
    finally:
        db.close()


def is_zombie(row: LeaseRow | None, *, grace_s: float = ZOMBIE_GRACE_S) -> bool:
    if row is None:
        return False
    if not _pid_alive(row.pid):
        return True
    exp = _parse(row.expires)
    if exp is None:
        return True
    return _now() >= exp + timedelta(seconds=grace_s)


@contextlib.contextmanager
def lease(
    task_id: str, *, ttl_s: int = DEFAULT_LEASE_TTL_S, interval_s: float = LEASE_REFRESH_S
) -> Iterator[Callable[[], bool]]:
    """Hold `task_id`'s dispatch lease for the block, kept warm by a daemon thread under
    a fresh token. Yields a predicate that is True once the lease has been lost (reaped
    and stolen, or cleared by the operator) so the body can bail. `claim()` raising
    `Leased` propagates."""
    token = new_token()
    claim(task_id, token, ttl_s=ttl_s)
    stop = threading.Event()
    lost = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval_s):
            try:
                refresh(task_id, token, ttl_s=ttl_s)
            except LeaseLost:
                lost.set()
                return
            except sqlite3.Error:
                continue  # transient DB lock -- try again next tick
    t = threading.Thread(target=_loop, name=f"lease-{task_id}", daemon=True)
    t.start()
    try:
        yield lost.is_set
    finally:
        stop.set()
        t.join(timeout=2.0)
        with contextlib.suppress(sqlite3.Error):
            release(task_id, token)