# SPDX-License-Identifier: Apache-2.0
"""Digest rendering."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from zecor.digest import render
from zecor.orchestrator import TaskOutcome


def _o(state: str, **kw: object) -> TaskOutcome:
    return TaskOutcome(task_id=kw.pop("tid", "0001"), state=state, **kw)  # type: ignore[arg-type]


def test_headline_counts(tmp_path: Path) -> None:
    outs = [_o("landed", tid="1"), _o("blocked", tid="2"), _o("deferred", tid="3")]
    text = render(outs, tmp_path / "none.db", day=date(2026, 8, 29))
    assert text.startswith("# 2026-08-29 - 3 tasks, 1 landed, 1 blocked, 1 deferred")


def test_landed_shows_pr_url_when_present(tmp_path: Path) -> None:
    o = _o("landed", tiers_used=["t1", "t3"], pr_url="https://github.com/x/y/pull/7")
    text = render([o], tmp_path / "none.db", day=date(2026, 8, 29))
    assert "https://github.com/x/y/pull/7" in text
    assert "t1 -> t3" in text


def test_blocked_surfaces_the_open_question(tmp_path: Path) -> None:
    from zecor.brief import Brief

    o = _o(
        "blocked",
        note="4 escalations, still red",
        brief=Brief("s", [], [], [], "should the token rotate on every 401?"),
        lane_branch="zecor/0001",
    )
    text = render([o], tmp_path / "none.db", day=date(2026, 8, 29))
    assert "Open question: should the token rotate" in text
    assert "Best attempt: zecor/0001" in text


def test_needs_human_and_gate_broken_go_under_needs_you(tmp_path: Path) -> None:
    outs = [_o("needs-human-design", note="judgment"), _o("gate-broken", note="127")]
    text = render(outs, tmp_path / "none.db", day=date(2026, 8, 29))
    assert "## Needs you" in text
    assert "needs design" in text
    assert "gate misconfigured" in text


def test_frontier_tokens_per_pr_is_reported(tmp_path: Path) -> None:
    o = _o("landed")
    o._frontier = (3000, 1000)
    text = render([o], tmp_path / "none.db", day=date(2026, 8, 29))
    assert "Frontier tokens per landed PR: 4000" in text


def test_render_from_ledger_rebuilds_from_the_attempt_table(tmp_path: Path) -> None:
    import sqlite3

    from zecor.digest import render_from_ledger

    db = tmp_path / "zecor.db"
    con = sqlite3.connect(db)
    con.executescript((Path(__file__).resolve().parents[1] / "setup" / "schema.sql").read_text())
    day = date(2026, 8, 29)
    con.executemany(
        "INSERT INTO attempt (task_id, tier, model, verify_exit, outcome, reason, "
        "in_tokens, out_tokens, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("0001", "t1", None, 1, "fail", "local", 0, 0, f"{day}T01:00:00"),
            ("0001", "t3", "terra", 0, "pass", "ladder", 1200, 300, f"{day}T01:05:00"),
            ("0002", "t1", None, 1, "fail", "local", 0, 0, f"{day}T02:00:00"),
        ],
    )
    con.commit()
    con.close()
    text = render_from_ledger(db, day)
    assert "2 task(s) touched, 1 reached a green gate" in text
    assert "0001  pass  (2 attempt(s), tiers t1 -> t3" in text
    assert "0002  fail" in text