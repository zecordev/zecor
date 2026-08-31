# SPDX-License-Identifier: Apache-2.0
"""core.replenish: roadmap -> queue provisioning (Endurance step 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zecor import config
from zecor.cards import Card, Klass
from zecor.core import replenish
from zecor.decompose import Decomposition

_ROADMAP = """# Mission: Test mission {slug: tm}
Why.

## Campaign: Test campaign {slug: tc}
Outcome.

### Move: First move {slug: mv-one}
Done when the first thing works.

### Move: Second move {slug: mv-two}
Done when the second thing works.
"""


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "ZECOR_ROOT", tmp_path)
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db" / "zecor.db")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "roadmap").mkdir()
    (tmp_path / "roadmap" / "tm.md").write_text(_ROADMAP)
    monkeypatch.setattr(replenish, "_known_repos", lambda: {"widget"})

    def _fake_plan(item: object, cid: str) -> Card:
        return Card(
            id=cid, repo=getattr(item, "repo", "widget"),
            intent=getattr(item, "intent", "do a thing"),
            verify_cmd=".fleet/verify.sh", allow_files=[], klass=Klass.BOUNDED,
        )

    monkeypatch.setattr("zecor.planner.plan_item", _fake_plan)
    return tmp_path


def _decomp(repo: str = "widget", n: int = 2, ready: bool = True) -> object:
    def _fn(_doc: str) -> Decomposition:
        return Decomposition(
            repo=repo,
            tasks=[{"intent": f"task {i}", "check": "pytest", "ready": ready} for i in range(n)],
            open_questions=[], tier="t4", raw="{}",
        )
    return _fn


# -- next_move (pure) ---------------------------------------------------------

def _views() -> list[object]:
    from zecor.core import view
    return view.cards()


def test_next_move_picks_the_first_unplanned() -> None:
    from zecor.core import roadmap
    ms = roadmap.load()
    mv = replenish.next_move(ms, [], stuck=set())
    assert mv is not None and mv.slug == "mv-one"


def test_next_move_skips_a_stuck_move() -> None:
    from zecor.core import roadmap
    ms = roadmap.load()
    mv = replenish.next_move(ms, [], stuck={"mv-one"})
    assert mv is not None and mv.slug == "mv-two"


def test_next_move_none_when_frontier_move_is_adequately_stocked() -> None:
    from zecor.core import roadmap, view
    ms = roadmap.load()
    for i in range(2):
        _write_card(f"010{i}", move="mv-one", state="ready")
    mv = replenish.next_move(ms, view.cards(), stuck=set(), low_water=2)
    assert mv is None  # mv-one has 2 live cards; don't jump to mv-two


def test_next_move_tops_up_a_thin_move() -> None:
    from zecor.core import roadmap, view
    ms = roadmap.load()
    _write_card("0100", move="mv-one", state="ready")
    mv = replenish.next_move(ms, view.cards(), stuck=set(), low_water=2)
    assert mv is not None and mv.slug == "mv-one"


# -- replenish_once --------------------------------------------------------------

def test_happy_path_plants_linked_cards() -> None:
    r = replenish.replenish_once(decompose_fn=_decomp(n=2), batch=3)
    assert r.acted and r.move == "mv-one" and len(r.created) == 2
    from zecor.core import view
    made = {c.id: c for c in view.cards()}
    assert all(made[cid].move == "mv-one" for cid in r.created)


def test_batch_caps_the_cards_added() -> None:
    r = replenish.replenish_once(decompose_fn=_decomp(n=9), batch=3)
    assert r.acted and len(r.created) == 3


def test_unknown_repo_marks_the_move_stuck() -> None:
    r = replenish.replenish_once(decompose_fn=_decomp(repo="not-a-repo"))
    assert not r.acted and r.stuck and "mv-one" in replenish.stuck_moves()


def test_no_usable_tasks_marks_stuck() -> None:
    r = replenish.replenish_once(decompose_fn=_decomp(n=0))
    assert not r.acted and r.stuck


def test_decompose_failure_rolls_back_and_raises() -> None:
    def _boom(_doc: str) -> Decomposition:
        raise RuntimeError("frontier down")

    with pytest.raises(replenish.ReplenishError):
        replenish.replenish_once(decompose_fn=_boom)
    from zecor.core import view
    assert view.cards() == []
    assert "mv-one" not in replenish.stuck_moves()  # transient, not stuck


def test_stale_running_op_is_rolled_back() -> None:
    _write_card("0100", move="mv-one", state="ready")
    db = replenish._db()
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    db.execute(
        "INSERT INTO replenish_op (move_slug, started_at, status, created_ids) "
        "VALUES ('mv-one', ?, 'running', '[\"0100\"]')", (old,),
    )
    db.commit()
    db.close()
    replenish._rollback_stale(datetime.now(UTC))
    assert not (config.TASKS_DIR / "0100.md").exists()  # pristine card removed


def test_stale_rollback_spares_a_touched_card() -> None:
    _write_card("0100", move="mv-one", state="ready", retry_count=1)
    db = replenish._db()
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    db.execute(
        "INSERT INTO replenish_op (move_slug, started_at, status, created_ids) "
        "VALUES ('mv-one', ?, 'running', '[\"0100\"]')", (old,),
    )
    db.commit()
    db.close()
    replenish._rollback_stale(datetime.now(UTC))
    assert (config.TASKS_DIR / "0100.md").exists()  # a retried card is left alone


def test_clear_stuck() -> None:
    replenish.replenish_once(decompose_fn=_decomp(repo="not-a-repo"))
    assert replenish.clear_stuck("mv-one") == 1
    assert "mv-one" not in replenish.stuck_moves()


def _write_card(cid: str, *, move: str, state: str, retry_count: int = 0) -> None:
    c = Card(
        id=cid, repo="widget", intent="x", verify_cmd=".fleet/verify.sh",
        allow_files=[], klass=Klass.BOUNDED, state=state, move=move, retry_count=retry_count,
    )
    (config.TASKS_DIR / f"{cid}.md").write_text(c.render())


def test_only_one_replenish_op_runs_at_a_time() -> None:
    from datetime import datetime as _dt

    db = replenish._db()
    db.execute(
        "INSERT INTO replenish_op (move_slug, started_at, status) VALUES ('mv-two', ?, 'running')",
        (_dt.now(UTC).isoformat(),),
    )
    db.commit()
    db.close()
    r = replenish.replenish_once(decompose_fn=_decomp(n=2))
    assert not r.acted and "another replenish" in r.reason


def test_a_crash_mid_planning_rolls_back_the_cards_it_made(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky_plan(item: object, cid: str) -> Card:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("planner exploded")
        return Card(
            id=cid, repo="widget", intent="x", verify_cmd=".fleet/verify.sh",
            allow_files=[], klass=Klass.BOUNDED,
        )

    monkeypatch.setattr("zecor.planner.plan_item", flaky_plan)
    with pytest.raises(replenish.ReplenishError):
        replenish.replenish_once(decompose_fn=_decomp(n=3))
    from zecor.core import view
    assert view.cards() == []  # the first card was rolled back