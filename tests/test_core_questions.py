# SPDX-License-Identifier: Apache-2.0
"""core.questions: NEEDS_HUMAN as a durable, de-duplicated message."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from zecor import config
from zecor.core import questions as q
from zecor.core import transcript


@dataclass
class FakeCard:
    id: str
    state: str


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db" / "zecor.db")


def test_scan_posts_one_question_and_a_transcript_turn() -> None:
    made = q.scan([FakeCard("0016", "blocked")], reason_fn=lambda _c: "cannot resolve symbol Foo")
    assert len(made) == 1
    ops = q.open_questions()
    assert [x.card_id for x in ops] == ["0016"] and "Foo" in ops[0].reason
    turns = transcript.tail()
    assert turns[-1].kind == "question" and turns[-1].card_id == "0016"


def test_scan_is_idempotent_for_the_same_reason() -> None:
    c = [FakeCard("0016", "blocked")]
    q.scan(c, reason_fn=lambda _c: "same reason")
    again = q.scan(c, reason_fn=lambda _c: "same reason")
    assert again == [] and len(q.open_questions()) == 1


def test_a_new_reason_is_a_new_question() -> None:
    c = [FakeCard("0016", "blocked")]
    q.scan(c, reason_fn=lambda _c: "reason one")
    q.scan(c, reason_fn=lambda _c: "reason two")
    assert len(q.open_questions()) == 2


def test_non_askable_states_are_ignored() -> None:
    assert q.scan([FakeCard("1", "ready"), FakeCard("2", "landed")]) == []


def test_answer_closes_the_question_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_retry(card_id: str, **kw: object) -> object:
        calls.append((card_id, kw.get("note")))  # type: ignore[arg-type]
        return type("R", (), {"ok": True, "message": "0016 re-queued"})()

    monkeypatch.setattr("zecor.core.intents.retry", fake_retry)
    (qid,) = q.scan([FakeCard("0016", "blocked")], reason_fn=lambda _c: "r")
    res = q.answer(qid, "  try   opus  instead ")
    assert res.ok and res.card_id == "0016"
    assert calls == [("0016", "try opus instead")]
    assert q.open_questions() == []
    assert transcript.tail()[-1].kind == "answer"


def test_a_failed_retry_reopens_the_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "zecor.core.intents.retry",
        lambda cid, **kw: type("R", (), {"ok": False, "message": "is 'landed'"})(),
    )
    (qid,) = q.scan([FakeCard("0016", "blocked")], reason_fn=lambda _c: "r")
    res = q.answer(qid, "do the thing")
    assert not res.ok and "retry refused" in res.message
    assert [x.id for x in q.open_questions()] == [qid]  # back open


def test_answer_twice_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "zecor.core.intents.retry",
        lambda cid, **kw: type("R", (), {"ok": True, "message": "ok"})(),
    )
    (qid,) = q.scan([FakeCard("0016", "blocked")], reason_fn=lambda _c: "r")
    assert q.answer(qid, "first").ok
    assert not q.answer(qid, "second").ok


def test_answer_for_card_needs_exactly_one_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "zecor.core.intents.retry",
        lambda cid, **kw: type("R", (), {"ok": True, "message": "ok"})(),
    )
    assert not q.answer_for_card("0016", "x").ok  # none open
    q.scan([FakeCard("0016", "blocked")], reason_fn=lambda _c: "a")
    q.scan([FakeCard("0016", "blocked")], reason_fn=lambda _c: "b")
    r = q.answer_for_card("0016", "x")
    assert not r.ok and "2 open questions" in r.message