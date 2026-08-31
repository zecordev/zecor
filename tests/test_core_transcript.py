# SPDX-License-Identifier: Apache-2.0
"""core.transcript: the persisted conversation (Console II spine)."""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zecor import config
from zecor.core import transcript as tr


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "db" / "zecor.db"
    monkeypatch.setattr(config, "DB_PATH", p)
    return p


def test_append_returns_monotonic_ids_and_tail_is_oldest_first() -> None:
    a = tr.append("you", "message", "first")
    b = tr.append("zecor", "reply", "second")
    assert b > a
    got = tr.tail()
    assert [t.text for t in got] == ["first", "second"]
    assert got[0].role == "you" and got[1].kind == "reply"


def test_unknown_role_and_kind_are_coerced_not_dropped() -> None:
    tr.append("banana", "wobble", "still here")
    (t,) = tr.tail()
    assert t.role == "zecor" and t.kind == "notice" and t.text == "still here"


def test_since_returns_only_newer_turns() -> None:
    tr.append("you", "message", "a")
    mark = tr.latest_id()
    tr.append("zecor", "reply", "b")
    tr.append("zecor", "narration", "c")
    assert [t.text for t in tr.since(mark)] == ["b", "c"]


def test_for_card_filters_by_card_id() -> None:
    tr.append("zecor", "question", "0016 needs you", card_id="0016")
    tr.append("you", "answer", "try opus", card_id="0016")
    tr.append("zecor", "narration", "unrelated")
    assert [t.text for t in tr.for_card("0016")] == ["0016 needs you", "try opus"]


def test_meta_round_trips() -> None:
    tr.append("zecor", "run", "started", meta={"pid": 4242, "tier": "t3"})
    (t,) = tr.tail()
    assert t.meta == {"pid": 4242, "tier": "t3"}


def test_prune_drops_old_turns_only() -> None:
    old = datetime.now(UTC) - timedelta(days=90)
    tr.append("you", "message", "ancient", now=old)
    tr.append("you", "message", "fresh")
    assert tr.prune(keep_days=30) == 1
    assert [t.text for t in tr.tail()] == ["fresh"]


def _hammer(args: tuple[str, int]) -> int:
    db_path, n = args
    os.environ["ZECOR_DB_OVERRIDE"] = db_path  # not used; kept for clarity
    from zecor import config as c
    from zecor.core import transcript as t

    c.DB_PATH = Path(db_path)
    for i in range(n):
        t.append("you", "message", f"m{i}")
    return n


def test_two_processes_appending_lose_nothing(_db: Path) -> None:
    _db.parent.mkdir(parents=True, exist_ok=True)
    tr.append("zecor", "notice", "seed")  # create the db/WAL first
    with ProcessPoolExecutor(max_workers=2) as pool:
        list(pool.map(_hammer, [(str(_db), 100), (str(_db), 100)]))
    assert tr.count() == 201
    ids = [t.id for t in tr.tail(1000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)  # monotonic, unique