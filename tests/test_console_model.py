# SPDX-License-Identifier: Apache-2.0
"""console.model: pure presentation logic (Command Center build step 3)."""

from __future__ import annotations

from zecor.console import model
from zecor.core.view import CardView, Health


def _cv(cid: str, state: str, **kw: object) -> CardView:
    base = dict(
        id=cid, repo="demo", intent=f"intent {cid}", state=state, klass="bounded",
        tag=None, retry_count=0, rev=0, depends_on=(), move=None, zombie=False,
        lease_pid=None, mtime=0.0, path=None,
    )
    base.update(kw)
    return CardView(**base)  # type: ignore[arg-type]


def test_age_buckets() -> None:
    assert model.age(0.0) == "-"
    assert model.age(1_000.0, now=1_012.0) == "12s"
    assert model.age(1_000.0, now=1_000.0 + 240) == "4m"
    assert model.age(1_000.0, now=1_000.0 + 3 * 3600) == "3h"
    assert model.age(1_000.0, now=1_000.0 + 2 * 86400) == "2d"


def test_rows_are_worst_state_first_then_id() -> None:
    cards = [
        _cv("0003", "ready"),
        _cv("0001", "landed"),
        _cv("0002", "blocked"),
        _cv("0005", "gate-broken"),
        _cv("0004", "blocked"),
    ]
    got = [r.key for r in model.rows(cards)]
    assert got == ["0005", "0002", "0004", "0003", "0001"]


def test_zombie_row_shows_zombie_in_the_state_cell() -> None:
    (row,) = model.rows([_cv("0001", "ready", zombie=True)])
    assert row.cells[1] == "ZOMBIE"
    assert row.zombie is True and row.state == "ready"


def test_snapshot_key_changes_only_on_a_meaningful_field() -> None:
    a = [_cv("0001", "ready", rev=1, mtime=10.0)]
    b = [_cv("0001", "ready", rev=1, mtime=10.0)]
    assert model.snapshot_key(a) == model.snapshot_key(b)
    c = [_cv("0001", "ready", rev=2, mtime=10.0)]
    assert model.snapshot_key(a) != model.snapshot_key(c)
    d = [_cv("0001", "blocked", rev=1, mtime=10.0)]
    assert model.snapshot_key(a) != model.snapshot_key(d)


def test_status_line_has_the_flag_and_counts() -> None:
    h = Health(
        stopped=True, disk_free_gb=812, disk_floor_gb=100, advised_lanes=1,
        max_lanes=2, counts={"ready": 3, "blocked": 1}, resource_note="",
    )
    s = model.status_line(h)
    assert "STOP" in s and "ready=3" in s and "blocked=1" in s and "lanes 1/2" in s
    assert "GO" in model.status_line(
        Health(False, 812, 100, 2, 2, {"ready": 1})
    )


def test_intent_is_clipped() -> None:
    long = _cv("0001", "ready", intent="x" * 200)
    (row,) = model.rows([long], intent_width=40)
    assert len(row.cells[4]) == 40 and row.cells[4].endswith("…")


def test_layout_for_width() -> None:
    assert model.layout_for(80) == "narrow"
    assert model.layout_for(120) == "medium"
    assert model.layout_for(160) == "wide"


def test_status_line_flags() -> None:
    h = Health(stopped=False, disk_free_gb=500, disk_floor_gb=100,
               advised_lanes=2, max_lanes=2, counts={"ready": 1})
    s = model.status_line(h, nlu_ok=False, repo_focus="widget", read_only=True)
    assert "NLU:off" in s and "widget" in s and "READ-ONLY" in s


def test_turn_line_formats_role_and_kind() -> None:
    line = model.turn_line("you", "message", "why is 0016 stuck?")
    assert "you" in line and "0016" in line
    q = model.turn_line("zecor", "question", "0016 needs you\nreason here")
    assert "?" in q and "reason here" in q


def test_momentum_cue() -> None:
    assert model.momentum(0, 0, "▁▁▁") == ""  # nothing landed yet
    m = model.momentum(17, 3, "▁▂▄█")
    assert "✦17" in m and "↑3 today" in m and "█" in m
    assert model.momentum(5, 0, "▁▁▁▁") == "✦5"  # flat sparkline dropped