# SPDX-License-Identifier: Apache-2.0
"""core.progress: roadmap progress evaluation + the weekly log."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zecor import config
from zecor.cards import Card, Klass
from zecor.core import progress, roadmap

_ROADMAP = """# Mission: M {slug: m}
.

## Campaign: C {slug: c}
.

### Move: One {slug: mv-one}
Done when one.

### Move: Two {slug: mv-two}
Done when two.
"""


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "ZECOR_ROOT", tmp_path)
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db" / "zecor.db")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "roadmap").mkdir()
    (tmp_path / "roadmap" / "m.md").write_text(_ROADMAP)
    return tmp_path


def _rolled(slug: str, status: str) -> roadmap.Rolled:
    return roadmap.Rolled("move", slug, slug.title(), status, 0, 0, 0.0)


def test_evaluate_detects_landed_and_started() -> None:
    before = [_rolled("mv-one", "in-progress"), _rolled("mv-two", "unplanned")]
    after = [_rolled("mv-one", "landed"), _rolled("mv-two", "ready")]
    ev = progress.evaluate(before, after, stuck_before=set(), stuck_after=set())
    kinds = {(e.kind, e.slug) for e in ev}
    assert ("landed", "mv-one") in kinds and ("started", "mv-two") in kinds


def test_evaluate_reports_newly_stuck_only() -> None:
    before = [_rolled("mv-one", "blocked")]
    after = [_rolled("mv-one", "blocked")]
    ev = progress.evaluate(
        before, after, stuck_before={"mv-two"}, stuck_after={"mv-one", "mv-two"}
    )
    assert [(e.kind, e.slug) for e in ev] == [("stuck", "mv-one")]


def test_detect_stuck_from_the_ledger() -> None:
    _card("0001", "mv-one")
    _card("0002", "mv-one")
    db = _attempt_schema()
    for _ in range(7):
        db.execute("INSERT INTO attempt (task_id, outcome) VALUES ('0001','fail')")
    db.commit()
    db.close()
    ms = roadmap.load()
    from zecor.core import view
    assert progress.detect_stuck(ms, view.cards(), k=6) == ["mv-one"]


def test_detect_stuck_ignores_a_move_with_a_land() -> None:
    _card("0001", "mv-one")
    db = _attempt_schema()
    for _ in range(6):
        db.execute("INSERT INTO attempt (task_id, outcome) VALUES ('0001','fail')")
    db.execute("INSERT INTO attempt (task_id, outcome) VALUES ('0001','pass')")
    db.commit()
    db.close()
    ms = roadmap.load()
    from zecor.core import view
    assert progress.detect_stuck(ms, view.cards(), k=6) == []


def test_run_once_writes_a_weekly_log_and_snapshots() -> None:
    _card("0001", "mv-one", state="landed")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    rep = progress.run_once(now=now)
    assert any(e.kind == "landed" and e.slug == "mv-one" for e in rep.events)
    assert rep.log_path is not None and rep.log_path.exists()
    assert "mv-one" in rep.log_path.read_text()
    # second run with no change -> no new events
    assert progress.run_once(now=now).events == []


def _attempt_schema() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH)
    db.execute(
        "CREATE TABLE attempt (id INTEGER PRIMARY KEY, task_id TEXT, tier TEXT, vendor TEXT, "
        "model TEXT, started_at TEXT, ended_at TEXT, in_tokens INT, out_tokens INT, "
        "verify_exit INT, outcome TEXT, reason TEXT)"
    )
    return db


def _card(cid: str, move: str, *, state: str = "ready") -> None:
    c = Card(
        id=cid, repo="widget", intent="x", verify_cmd=".fleet/verify.sh",
        allow_files=[], klass=Klass.BOUNDED, state=state, move=move,
    )
    (config.TASKS_DIR / f"{cid}.md").write_text(c.render())