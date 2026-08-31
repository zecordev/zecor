# SPDX-License-Identifier: Apache-2.0
"""core.actionlog: the audit log and the deliberately narrow :undo."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import config
from zecor.cards import Card, Klass
from zecor.core import actionlog, intents


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "ZECOR_ROOT", tmp_path)
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db" / "zecor.db")
    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "STOP")
    monkeypatch.setattr(config, "QUEUE_INBOX", tmp_path / "queue" / "inbox.md")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "queue").mkdir()
    return tmp_path


def _card(cid: str, *, move: str | None = None) -> None:
    c = Card(
        id=cid, repo="widget", intent="x", verify_cmd=".fleet/verify.sh",
        allow_files=[], klass=Klass.BOUNDED, state="ready", move=move,
    )
    (config.TASKS_DIR / f"{cid}.md").write_text(c.render())


def test_every_mutating_verb_is_logged() -> None:
    _card("0001")
    intents.stop()
    intents.go()
    intents.assign_move("0001", "some-move")
    intents.add_inbox("widget", "do a thing")
    verbs = [a.verb for a in actionlog.recent()]
    assert verbs[:4] == ["add_inbox", "assign_move", "go", "stop"]  # newest first


def test_undo_nothing() -> None:
    assert actionlog.undo_last().message == "nothing to undo"


def test_undo_stop_does_not_relog() -> None:
    intents.stop()
    assert config.stop_requested()
    r = actionlog.undo_last()
    assert r.ok and not config.stop_requested()
    # undo must NOT log a fresh reversible action -- a second undo walks back, it does
    # not toggle STOP on again.
    assert actionlog.undo_last().message == "nothing to undo"
    assert not config.stop_requested()


def test_undo_go_re_sets_stop() -> None:
    config.STOP_FILE.write_text("halt\n")
    intents.go()
    assert not config.stop_requested()
    assert actionlog.undo_last().ok and config.stop_requested()


def test_undo_assign_move_restores_the_prior_move() -> None:
    _card("0001", move="old-move")
    intents.assign_move("0001", "new-move")
    from zecor.core import view
    assert view.one("0001").move == "new-move"
    assert actionlog.undo_last().ok
    assert view.one("0001").move == "old-move"


def test_undo_assign_move_refuses_if_the_card_moved_since() -> None:
    _card("0001", move="old")
    intents.assign_move("0001", "new")
    # someone else bumps the card's rev
    from zecor.core import cards as core_cards
    core_cards.mutate("0001", lambda c: setattr(c, "state", "blocked"))
    r = actionlog.undo_last()
    assert not r.ok and "changed since" in r.message


def test_undo_add_inbox_removes_an_unplanned_line() -> None:
    intents.add_inbox("widget", "build the widget")
    assert "build the widget" in config.QUEUE_INBOX.read_text()
    assert actionlog.undo_last().ok
    assert "build the widget" not in config.QUEUE_INBOX.read_text()


def test_undo_add_inbox_refuses_once_planned() -> None:
    intents.add_inbox("widget", "build the widget")
    txt = config.QUEUE_INBOX.read_text().replace(
        "- widget: build the widget", "# planned: - widget: build the widget"
    )
    config.QUEUE_INBOX.write_text(txt)
    r = actionlog.undo_last()
    assert not r.ok and "already planned" in r.message


def test_retry_and_clear_zombie_are_not_undoable() -> None:
    _card("0001")
    intents.retry("0001", note="again")  # 0001 is 'ready' -> not RETRYABLE, but still logged? no
    # retry on a non-retryable card returns early WITHOUT logging; use a blocked card
    from zecor.core import cards as core_cards
    core_cards.mutate("0001", lambda c: setattr(c, "state", "blocked"))
    intents.retry("0001", note="again")
    r = actionlog.undo_last()
    assert not r.ok and "cannot be undone" in r.message