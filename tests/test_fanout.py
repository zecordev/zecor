# SPDX-License-Identifier: Apache-2.0
"""Feature fan-out -- one requirement, a card per client repo (roadmap: feature-fanout)."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import config
from zecor.cards import Card
from zecor.core import cards as core_cards
from zecor.core import intents


@pytest.fixture(autouse=True)
def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "zecor"
    for name, sub in [
        ("ZECOR_ROOT", ""), ("TASKS_DIR", "tasks"), ("QUEUE_INBOX", "queue/inbox.md"),
        ("DB_PATH", "db/zecor.db"), ("STOP_FILE", "STOP"), ("LOGS_DIR", "logs"),
    ]:
        monkeypatch.setattr(config, name, root / sub if sub else root)
    (root / "tasks").mkdir(parents=True)
    monkeypatch.setattr(
        intents, "repo_names",
        lambda: ["resolve-web", "resolve-ios", "resolve-droid"],
    )
    # plan_item hits a local model -- stub it to a plain card
    import zecor.planner as planner

    def fake_plan_item(item, next_id):  # type: ignore[no-untyped-def]
        return Card(next_id, item.repo, item.intent, ".fleet/verify.sh", ["src/x"])

    monkeypatch.setattr(planner, "plan_item", fake_plan_item)
    return root


def test_fanout_creates_one_card_per_repo_with_a_shared_id() -> None:
    res = intents.fanout(
        "add a rest-timer to the superset screen",
        ["resolve-web", "resolve-ios", "resolve-droid"],
        move="macro-logbook",
    )
    assert res.ok
    fid = res.data["fanout"]
    cards = [core_cards.read(f"{i:04d}") for i in (1, 2, 3)]
    assert {c.repo for c in cards} == {"resolve-web", "resolve-ios", "resolve-droid"}
    assert all(c.fanout == fid for c in cards)
    assert all(c.move == "macro-logbook" for c in cards)
    assert all("rest-timer" in c.intent for c in cards)


def test_fanout_rejects_unknown_repos_and_singletons() -> None:
    assert intents.fanout("x", ["resolve-web", "nope"]).ok is False
    assert intents.fanout("x", ["resolve-web"]).ok is False  # need >= 2


def test_fanout_status_reports_states_and_flags_divergence() -> None:
    res = intents.fanout("x y z", ["resolve-web", "resolve-ios"])
    fid = res.data["fanout"]

    s1 = intents.fanout_status(fid)
    assert s1.ok and "landed" not in s1.message  # both ready -> in step

    core_cards.mutate("0001", lambda c: setattr(c, "state", "landed"))
    s2 = intents.fanout_status(fid)
    assert s2.ok is False and "DIVERGED" in s2.message

    core_cards.mutate("0002", lambda c: setattr(c, "state", "landed"))
    s3 = intents.fanout_status(fid)
    assert s3.ok is True  # both landed -> back in step


def test_fanout_status_unknown_id() -> None:
    assert intents.fanout_status("fo-nope").ok is False