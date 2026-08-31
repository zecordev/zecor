# SPDX-License-Identifier: Apache-2.0
"""core.view: the read model (Command Center build step 2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zecor import config
from zecor.cards import Card
from zecor.core import view


@pytest.fixture(autouse=True)
def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "zecor"
    for name, sub in [
        ("ZECOR_ROOT", ""), ("TASKS_DIR", "tasks"), ("DIGEST_DIR", "digest"),
        ("DB_PATH", "db/zecor.db"), ("STOP_FILE", "STOP"),
    ]:
        monkeypatch.setattr(config, name, root / sub if sub else root)
    (root / "tasks").mkdir(parents=True)
    return root


def _card(cid: str, state: str = "ready", **kw: object) -> None:
    c = Card(cid, "demo", f"card {cid}", "./v.sh", ["a.py"], **kw)  # type: ignore[arg-type]
    c.state = state
    c.write(config.TASKS_DIR)


def test_cards_and_counts() -> None:
    _card("0001", "ready")
    _card("0002", "blocked")
    _card("0003", "landed")
    rows = view.cards()
    assert [r.id for r in rows] == ["0001", "0002", "0003"]
    assert view.card_counts() == {"ready": 1, "blocked": 1, "landed": 1}


def test_unreadable_card_is_surfaced_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    _card("0009", "ready")
    # force a parse failure without depending on what Card.parse tolerates
    monkeypatch.setattr(
        view.Card, "parse", staticmethod(lambda _t: (_ for _ in ()).throw(ValueError("boom")))
    )
    rows = {r.id: r for r in view.cards()}
    assert rows["0009"].state == "unreadable"
    assert view.card_counts()["unreadable"] == 1


def test_zombie_shows_in_counts() -> None:
    from zecor.core import leases

    _card("0001", "ready")
    db = leases._db()
    db.execute(
        "INSERT INTO lease VALUES ('0001','tok',999999,'2000-01-01T00:00:00+00:00','now')"
    )
    db.commit()
    db.close()
    assert view.card_counts().get("zombie") == 1
    assert view.cards()[0].zombie is True


def test_quota_and_attempts_tolerate_a_missing_db() -> None:
    assert view.quota() == []
    assert view.attempts() == []
    assert view.latest_digest() == "(no digest yet)"


def test_quota_and_attempts_read_a_real_db() -> None:
    config.DB_PATH.parent.mkdir(parents=True)
    db = sqlite3.connect(config.DB_PATH)
    db.executescript(
        "CREATE TABLE quota (vendor TEXT, window TEXT, resets_at TEXT, used INT, cap INT);"
        "CREATE TABLE attempt (id INTEGER PRIMARY KEY, task_id TEXT, tier TEXT, vendor TEXT,"
        " model TEXT, started_at TEXT, ended_at TEXT, in_tokens INT, out_tokens INT,"
        " verify_exit INT, outcome TEXT, reason TEXT);"
    )
    db.execute("INSERT INTO quota VALUES ('anthropic','5h','2026-01-01',3,NULL)")
    db.execute(
        "INSERT INTO attempt (task_id, tier, model, started_at, verify_exit, outcome, reason)"
        " VALUES ('0001','t4','opus','2026-08-29T00:00:00',0,'pass','forced')"
    )
    db.commit()
    db.close()
    q = view.quota()
    assert q[0].vendor == "anthropic" and q[0].cap is None and q[0].used == 3
    a = view.attempts("0001")
    assert a[0].tier == "t4" and a[0].outcome == "pass"


def test_attempts_tolerate_null_columns() -> None:
    config.DB_PATH.parent.mkdir(parents=True)
    db = sqlite3.connect(config.DB_PATH)
    db.executescript(
        "CREATE TABLE attempt (id INTEGER PRIMARY KEY, task_id TEXT, tier TEXT, vendor TEXT,"
        " model TEXT, started_at TEXT, ended_at TEXT, in_tokens INT, out_tokens INT,"
        " verify_exit INT, outcome TEXT, reason TEXT);"
    )
    db.execute("INSERT INTO attempt (task_id) VALUES ('0001')")  # everything else NULL
    db.commit()
    db.close()
    (a,) = view.attempts("0001")
    assert a.tier == "?" and a.outcome == "?" and a.model is None and a.verify_exit == -1


def test_health_is_defensive() -> None:
    h = view.health()
    assert h.stopped is False
    assert h.max_lanes >= 1
    assert isinstance(h.counts, dict)