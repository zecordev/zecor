# SPDX-License-Identifier: Apache-2.0
"""zecor.supervise: the keep-alive loop + the always-on watchers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zecor import config, supervise
from zecor.cards import Card, Klass


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "ZECOR_ROOT", tmp_path)
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db" / "zecor.db")
    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "STOP")
    monkeypatch.setattr(config, "QUEUE_INBOX", tmp_path / "queue" / "inbox.md")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "roadmap").mkdir()
    (tmp_path / "roadmap" / "m.md").write_text(
        "# Mission: M {slug: m}\n.\n\n## Campaign: C {slug: c}\n.\n\n"
        "### Move: One {slug: mv-one}\nDone when one.\n"
    )
    return tmp_path


def _card(cid: str, *, state: str = "ready", move: str | None = None) -> None:
    c = Card(
        id=cid, repo="widget", intent="x", verify_cmd=".fleet/verify.sh",
        allow_files=[], klass=Klass.BOUNDED, state=state, move=move,
    )
    (config.TASKS_DIR / f"{cid}.md").write_text(c.render())


def _attempt_db() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH)
    db.execute(
        "CREATE TABLE attempt (id INTEGER PRIMARY KEY, task_id TEXT, tier TEXT, vendor TEXT, "
        "model TEXT, started_at TEXT, ended_at TEXT, in_tokens INT, out_tokens INT, "
        "verify_exit INT, outcome TEXT, reason TEXT)"
    )
    return db


def test_watch_tick_posts_a_question_for_a_blocked_card() -> None:
    _card("0001", state="blocked")
    supervise.watch_tick()
    from zecor.core import questions
    assert [q.card_id for q in questions.open_questions()] == ["0001"]


def test_watch_tick_narrates_new_attempts_once() -> None:
    _card("0001")
    db = _attempt_db()
    db.execute("INSERT INTO attempt (task_id, tier, model, outcome, reason) "
               "VALUES ('0001','t3','codex','pass','gate green')")
    db.commit()
    db.close()
    supervise.watch_tick()
    from zecor.core import transcript
    narr = [t for t in transcript.tail() if t.kind == "narration"]
    assert len(narr) == 1 and "0001 -> t3" in narr[0].text and "gate green" in narr[0].text
    # a second tick does not re-narrate
    supervise.watch_tick()
    narr2 = [t for t in transcript.tail() if t.kind == "narration"]
    assert len(narr2) == 1


def test_loop_once_runs_a_slice_and_writes_a_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    _card("0001")
    monkeypatch.setattr(
        "zecor.core.budget.ceiling_state",
        lambda **k: type("S", (), {"state": "ok", "should_pause": False, "reason": "ok"})(),
    )
    calls: list[float] = []
    rc = supervise.supervise_loop(
        once=True, run_slice=lambda h: calls.append(h) or 0, sleep=lambda _s: None,
    )
    assert rc == 0 and calls == [supervise.RUN_SLICE_HOURS]
    hb = supervise.read_heartbeat()
    assert hb.get("ceiling") == "ok" and "pid" in hb


def test_loop_respects_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    config.STOP_FILE.write_text("halt\n")
    monkeypatch.setattr(
        "zecor.core.budget.ceiling_state",
        lambda **k: type("S", (), {"state": "ok", "should_pause": False, "reason": "ok"})(),
    )
    ran: list[float] = []
    supervise.supervise_loop(
        once=True, run_slice=lambda h: ran.append(h) or 0, sleep=lambda _s: None,
    )
    assert ran == []  # STOP present -> no slice


def test_loop_pauses_and_stops_on_the_spend_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "zecor.core.budget.ceiling_state",
        lambda **k: type(
            "S", (), {"state": "pause", "should_pause": True, "reason": "weekly cap"}
        )(),
    )
    ran: list[float] = []
    supervise.supervise_loop(
        once=True, run_slice=lambda h: ran.append(h) or 0, sleep=lambda _s: None,
    )
    assert ran == [] and config.STOP_FILE.exists()
    assert "weekly cap" in config.STOP_FILE.read_text()


def test_replenish_fires_when_the_queue_is_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "zecor.core.budget.ceiling_state",
        lambda **k: type("S", (), {"state": "ok", "should_pause": False, "reason": "ok"})(),
    )
    seen: list[int] = []
    monkeypatch.setattr(
        "zecor.core.replenish.replenish_once",
        lambda **kw: seen.append(kw.get("low_water", 0)) or type("R", (), {"acted": True})(),
    )
    supervise.supervise_loop(
        once=True, low_water=2, run_slice=lambda h: 0, sleep=lambda _s: None
    )
    assert seen == [2]  # 0 ready cards < low_water -> replenish called