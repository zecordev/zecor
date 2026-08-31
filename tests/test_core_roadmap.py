# SPDX-License-Identifier: Apache-2.0
"""core.roadmap: tree parse + computed rollup (Command Center build step 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import config
from zecor.core import roadmap
from zecor.core.view import CardView

DOC = """\
# Mission: Stay sharp {slug: staying-sharp}
Why this exists.

## Campaign: The fleet reviews its own work {slug: self-review}
One shippable outcome.

### Move: Deep self-review {slug: deep-review}
Done when a reverted-impl green gate is a block.

### Move: Rule mining {slug: rule-mining}
Recurring fixes become codemods.

## Campaign: Command Center {slug: command-center}

### Move: The console {slug: console}
"""


@pytest.fixture(autouse=True)
def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "zecor"
    (root / "roadmap").mkdir(parents=True)
    monkeypatch.setattr(config, "ZECOR_ROOT", root)
    (root / "roadmap" / "staying-sharp.md").write_text(DOC)
    return root


def _cv(cid: str, state: str, move: str | None, mtime: float = 100.0) -> CardView:
    return CardView(
        id=cid, repo="demo", intent="x", state=state, klass="bounded", tag=None,
        retry_count=0, rev=0, depends_on=(), move=move, zombie=False, lease_pid=None,
        mtime=mtime, path=None,  # type: ignore[arg-type]
    )


def test_parse_builds_the_four_levels() -> None:
    (mission,) = roadmap.load()
    assert mission.kind == "mission" and mission.slug == "staying-sharp"
    assert [c.slug for c in mission.children] == ["self-review", "command-center"]
    sr = mission.children[0]
    assert [m.slug for m in sr.children] == ["deep-review", "rule-mining"]
    assert "reverted-impl" in sr.children[0].body


def test_slug_is_derived_when_untagged() -> None:
    nodes = roadmap._parse_file("## Campaign: Some Big Thing\n")
    assert nodes[0].slug == "some-big-thing"


def test_rollup_folds_worst_first_and_marks_unplanned() -> None:
    missions = roadmap.load()
    cards = [
        _cv("0001", "landed", "deep-review"),
        _cv("0002", "blocked", "deep-review"),
        _cv("0003", "landed", "rule-mining"),
        # command-center campaign: no cards -> unplanned
    ]
    (rolled,) = roadmap.rollup(missions, cards)
    assert rolled.slug == "staying-sharp"
    sr, cc = rolled.children
    assert sr.slug == "self-review"
    assert sr.status == "blocked"  # deep-review has a blocked card -> worst wins
    assert sr.total == 3 and sr.landed == 2
    dr, rm = sr.children
    assert dr.status == "blocked" and rm.status == "landed"
    assert cc.status == "unplanned" and cc.total == 0


def test_rollup_in_progress_when_some_but_not_all_landed() -> None:
    missions = roadmap.load()
    cards = [_cv("1", "landed", "deep-review"), _cv("2", "ready", "deep-review")]
    (rolled,) = roadmap.rollup(missions, cards)
    dr = rolled.children[0].children[0]
    assert dr.status == "in-progress"


def test_orphan_cards_flags_unknown_or_missing_move() -> None:
    missions = roadmap.load()
    cards = [
        _cv("0001", "ready", "deep-review"),
        _cv("0002", "ready", "no-such-move"),
        _cv("0003", "ready", None),
    ]
    assert roadmap.orphan_cards(missions, cards) == ["0002", "0003"]


def test_load_tolerates_a_missing_roadmap_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "ZECOR_ROOT", tmp_path / "nope")
    assert roadmap.load() == []


def test_load_skips_a_plain_doc_without_a_mission_heading(_root: Path) -> None:
    (config.ZECOR_ROOT / "roadmap" / "PLAN.md").write_text(
        "# Some design plan\n\n## 1. Goal\n\n### A sub-heading\n\nprose\n"
    )
    slugs = {m.slug for m in roadmap.load()}
    assert slugs == {"staying-sharp"}  # PLAN.md's ## headings are NOT campaigns