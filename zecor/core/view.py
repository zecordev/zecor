# SPDX-License-Identifier: Apache-2.0
"""The read model -- Command Center build step 2.

Every surface reads zecor state through this one module, so there's no second query to
drift; every function here is defensive, surfacing a card that won't parse as
`state=unreadable` rather than crashing the caller. Reads only -- mutation lives in
`core.cards` and `core.intents`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from zecor import config
from zecor.cards import Card
from zecor.core import leases

TERMINAL_STATES = ("landed",)


@dataclass(frozen=True)
class CardView:
    id: str
    repo: str
    intent: str
    state: str
    klass: str
    tag: str | None
    retry_count: int
    rev: int
    depends_on: tuple[str, ...]
    move: str | None  # roadmap membership -- the slug of the Move this card is under
    zombie: bool
    lease_pid: int | None
    mtime: float
    path: Path
    fanout: str | None = None  # shared id for a fan-out group of cross-repo cards


def _card_files() -> list[Path]:
    try:
        return sorted(config.TASKS_DIR.glob("[0-9]*.md"))
    except OSError:
        return []


def _view_of(path: Path, lease: leases.LeaseRow | None) -> CardView:
    try:
        c = Card.parse(path.read_text())
    except (OSError, ValueError, KeyError):
        return CardView(
            id=path.stem, repo="?", intent="(card will not parse)", state="unreadable",
            klass="?", tag=None, retry_count=0, rev=0, depends_on=(), move=None,
            zombie=False, lease_pid=None, mtime=_mtime(path), path=path,
        )
    return CardView(
        id=c.id or path.stem,
        repo=c.repo,
        intent=c.intent,
        state=c.state,
        klass=c.klass.value,
        tag=c.tag,
        retry_count=c.retry_count,
        rev=c.rev,
        depends_on=tuple(c.depends_on),
        move=c.move or c.extra.get("move"),
        fanout=c.fanout,
        zombie=leases.is_zombie(lease),
        lease_pid=lease.pid if lease else None,
        mtime=_mtime(path),
        path=path,
    )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _leases() -> dict[str, leases.LeaseRow]:
    try:
        return leases.active()
    except Exception:  # a locked/corrupt db must not blank the board
        return {}


def cards() -> list[CardView]:
    ls = _leases()
    return [_view_of(p, ls.get(p.stem)) for p in _card_files()]


def one(card_id: str) -> CardView | None:
    p = config.TASKS_DIR / f"{card_id}.md"
    if not p.exists():
        return None
    return _view_of(p, leases.get(card_id))


def card_counts() -> dict[str, int]:
    """Bucket -> count, mutually exclusive (they sum to the card total). A card whose
    lease outlived its run counts as `zombie`, not as its stale on-disk state."""
    out: dict[str, int] = {}
    for cv in cards():
        bucket = "zombie" if cv.zombie else cv.state
        out[bucket] = out.get(bucket, 0) + 1
    return out


@dataclass(frozen=True)
class QuotaView:
    vendor: str
    window: str
    used: int
    cap: int | None
    resets_at: str


def _connect() -> sqlite3.Connection | None:
    if not config.DB_PATH.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5.0)
        db.row_factory = sqlite3.Row
        return db
    except sqlite3.Error:
        return None


def quota() -> list[QuotaView]:
    db = _connect()
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT vendor, window, used, cap, resets_at FROM quota ORDER BY vendor, window"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [
        QuotaView(r["vendor"], r["window"], int(r["used"] or 0),
                  None if r["cap"] is None else int(r["cap"]), str(r["resets_at"] or ""))
        for r in rows
    ]


@dataclass(frozen=True)
class AttemptView:
    task_id: str
    tier: str
    model: str | None
    outcome: str
    verify_exit: int
    started_at: str
    reason: str


def attempts(card_id: str | None = None, *, limit: int = 30) -> list[AttemptView]:
    db = _connect()
    if db is None:
        return []
    q = (
        "SELECT task_id, tier, model, outcome, verify_exit, started_at, reason "
        "FROM attempt "
    )
    args: tuple[object, ...] = ()
    if card_id is not None:
        q += "WHERE task_id = ? "
        args = (card_id,)
    q += "ORDER BY started_at DESC, id DESC LIMIT ?"
    args = (*args, limit)
    try:
        rows = db.execute(q, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [
        AttemptView(
            str(r["task_id"] or "?"), str(r["tier"] or "?"),
            None if r["model"] is None else str(r["model"]),
            str(r["outcome"] or "?"),
            int(r["verify_exit"] if r["verify_exit"] is not None else -1),
            str(r["started_at"] or ""), str(r["reason"] or ""),
        )
        for r in rows
    ]


def latest_digest() -> str:
    try:
        files = sorted(config.DIGEST_DIR.glob("*.md"))
    except OSError:
        files = []
    if not files:
        return "(no digest yet)"
    try:
        return files[-1].read_text()
    except OSError:
        return "(digest unreadable)"


def disk_free_gb() -> int:
    import shutil

    try:
        return shutil.disk_usage(Path.home()).free // (1024**3)
    except OSError:
        return -1


@dataclass(frozen=True)
class Health:
    stopped: bool
    disk_free_gb: int
    disk_floor_gb: int
    advised_lanes: int
    max_lanes: int
    counts: dict[str, int] = field(default_factory=dict)
    resource_note: str = ""


def health() -> Health:
    advised, note = _advice()
    return Health(
        stopped=config.stop_requested(),
        disk_free_gb=disk_free_gb(),
        disk_floor_gb=config.DISK_FLOOR_GB,
        advised_lanes=advised,
        max_lanes=config.MAX_CONCURRENT_LANES,
        counts=card_counts(),
        resource_note=note,
    )


def _advice() -> tuple[int, str]:
    try:
        from zecor.resources import advise_lanes, snapshot

        snap = snapshot()
        return advise_lanes(snap=snap), getattr(snap, "note", "")
    except Exception as e:  # a probe blowing up must not blank the whole view
        return config.MAX_CONCURRENT_LANES, f"resource probe failed: {e}"