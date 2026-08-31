# SPDX-License-Identifier: Apache-2.0
"""Lane = git worktree per task."""

from __future__ import annotations

from pathlib import Path

from tests.fakes import make_repo
from zecor.lanes import close_lane, open_lane


def test_open_creates_a_worktree_on_a_fleet_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    lane = open_lane("0001", repo)
    try:
        assert lane.path.is_dir()
        assert (lane.path / "src.txt").exists()
        assert lane.branch == "zecor/0001"
        hook = lane.path / ".git"
        # worktree .git is a file; the pre-push hook lands in the real gitdir
        assert hook.exists()
    finally:
        close_lane(lane, delete_branch=True)
    assert not lane.path.exists()


def test_diff_helpers_track_working_tree_changes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    lane = open_lane("0002", repo)
    try:
        (lane.path / "src.txt").write_text("// v0\n// added line\n")
        assert "src.txt" in lane.touched_files()
        assert lane.lines_changed() >= 1
        h1 = lane.diff_hash()
        (lane.path / "src.txt").write_text("// v0\n")  # revert
        assert lane.diff_hash() != h1
        assert lane.touched_files() == frozenset()
    finally:
        close_lane(lane, delete_branch=True)


def test_sandbox_policy_file_never_enters_a_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    lane = open_lane("0004", repo)
    try:
        (lane.path / "src.txt").write_text("// v0\n// real change\n")
        (lane.path / ".zecor-sandbox.policy").write_text("workdir = /x\n")
        assert ".zecor-sandbox.policy" not in lane.touched_files()
        lane.commit("real change")
        assert ".zecor-sandbox.policy" not in lane.committed_diff()
    finally:
        close_lane(lane, delete_branch=True)


def test_reset_discards_changes_but_keeps_the_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    lane = open_lane("0003", repo)
    try:
        (lane.path / "src.txt").write_text("// junk\n")
        (lane.path / "new.txt").write_text("stray\n")
        lane.reset()
        assert (lane.path / "src.txt").read_text() == "// v0\n"
        assert not (lane.path / "new.txt").exists()
    finally:
        close_lane(lane, delete_branch=True)