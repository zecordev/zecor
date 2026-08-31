# SPDX-License-Identifier: Apache-2.0
"""zecor.gitengine -- in-process git via the Rust crate, with a `git` fallback."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from zecor import _rustbridge, gitengine


def _git(d: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(d), *args], check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": _path()},
    )


def _path() -> str:
    import os
    return os.environ.get("PATH", "/usr/bin:/bin")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "r"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    (d / "a.txt").write_text("one\n")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "c1")
    _git(d, "checkout", "-q", "-b", "feature")
    (d / "b.txt").write_text("two\n")
    _git(d, "add", ".")
    _git(d, "commit", "-qm", "c2")
    return d


@pytest.mark.parametrize("force_python", [False, True])
def test_branch_state(repo: Path, force_python: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    if force_python:
        monkeypatch.setenv("ZECOR_NO_RUST", "1")
    elif _rustbridge.binary("zecor-gitengine") is None:
        pytest.skip("zecor-gitengine not built")
    _rustbridge.binary.cache_clear()
    s = gitengine.branch_state(repo, "feature", "main")
    assert s is not None
    assert (s.ahead, s.behind) == (1, 0)
    assert not s.is_merged and s.is_descendant
    assert s.merge_base == s.base_sha
    _rustbridge.binary.cache_clear()


def test_rev_parse_and_changed_files(repo: Path) -> None:
    sha = gitengine.rev_parse(repo, "HEAD")
    assert sha and len(sha) == 40
    assert gitengine.changed_files(repo, "main", "feature") == ["b.txt"]


def test_merged_branch(repo: Path) -> None:
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "m")
    s = gitengine.branch_state(repo, "feature", "main")
    assert s is not None and s.ahead == 0 and s.is_merged


@pytest.mark.parametrize("force_python", [False, True])
def test_log(repo: Path, force_python: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    if force_python:
        monkeypatch.setenv("ZECOR_NO_RUST", "1")
    elif _rustbridge.binary("zecor-gitengine") is None:
        pytest.skip("zecor-gitengine not built")
    _rustbridge.binary.cache_clear()
    entries = gitengine.log(repo, "HEAD", 10)
    assert [e.summary for e in entries] == ["c2", "c1"]
    assert entries[0].parents == [entries[1].sha]
    assert entries[0].author == "t"
    assert len(gitengine.log(repo, "HEAD", 1)) == 1
    _rustbridge.binary.cache_clear()


@pytest.mark.parametrize("force_python", [False, True])
def test_status(repo: Path, force_python: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    if force_python:
        monkeypatch.setenv("ZECOR_NO_RUST", "1")
    elif _rustbridge.binary("zecor-gitengine") is None:
        pytest.skip("zecor-gitengine not built")
    _rustbridge.binary.cache_clear()
    assert gitengine.status(repo).is_clean  # type: ignore[union-attr]

    (repo / "a.txt").write_text("one\nchanged\n")
    (repo / "fresh.txt").write_text("hi\n")
    st = gitengine.status(repo)
    assert st is not None and not st.is_clean
    assert st.untracked == ["fresh.txt"]
    assert any(e.path == "a.txt" and e.change == "modified" for e in st.unstaged)

    _git(repo, "add", "fresh.txt")
    st = gitengine.status(repo)
    assert st is not None
    assert any(e.path == "fresh.txt" and e.change == "added" for e in st.staged)
    _rustbridge.binary.cache_clear()
