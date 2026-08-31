# SPDX-License-Identifier: Apache-2.0
"""zecor plan -- the batched queue review."""

from __future__ import annotations

import pytest

from zecor import planner
from zecor.cards import Card
from zecor.planner import review_queue
from zecor.task import Klass


def mk(id_: str, klass: Klass = Klass.BOUNDED) -> Card:
    return Card(id_, "demo", f"task {id_}", "./.fleet/verify.sh", [], klass=klass)


def test_single_card_is_never_reviewed() -> None:
    cards, note = review_queue([mk("0001")])
    assert note == ""


def test_queue_review_applies_klass_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = (
        '{"adjustments": ['
        '{"id": "0001", "klass": "unbounded", "verify_cmd": null, "flag": null, "why": "x"},'
        '{"id": "0002", "klass": null, "verify_cmd": null, "flag": "split", "why": "big"}]}'
    )
    monkeypatch.setattr(planner, "ask", lambda p, **k: (reply, "t4"))
    cards, note = review_queue([mk("0001"), mk("0002")])
    assert cards[0].klass is Klass.UNBOUNDED
    assert cards[1].state == "needs-human-design"
    assert "1 flagged" in note and "split" in note


def test_queue_review_no_op_without_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    from zecor.frontier import NoFrontierAvailable

    def boom(p: str, **k: object) -> tuple[str, str]:
        raise NoFrontierAvailable("none")

    monkeypatch.setattr(planner, "ask", boom)
    cards, note = review_queue([mk("0001"), mk("0002")])
    assert cards[0].klass is Klass.BOUNDED  # unchanged
    assert "skipped" in note


def test_link_file_overlaps_serialises_same_file_cards() -> None:
    from zecor.planner import _link_file_overlaps

    a = Card("0001", "demo", "add feature A", "./.fleet/verify.sh", ["src/store.py"])
    b = Card("0002", "demo", "add feature B", "./.fleet/verify.sh", ["src/store.py", "src/x.py"])
    c = Card("0003", "other", "unrelated", "./.fleet/verify.sh", ["src/store.py"])  # diff repo
    notes = _link_file_overlaps([b, a, c])
    assert a.depends_on == []          # earliest, nothing before it
    assert b.depends_on == ["0001"]    # shares src/store.py with 0001
    assert c.depends_on == []          # different repo, no link
    assert notes and "0002 after 0001" in notes[0]