# SPDX-License-Identifier: Apache-2.0
"""Git worktree per task -- a worktree of the *target* repo on a fresh branch, under
`lanes/<id>/`. A pre-push hook refuses main/master."""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from zecor.config import LANES_DIR, REPO_ROOT

_PRE_PUSH = REPO_ROOT / "setup" / "pre-push"


class LaneError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise LaneError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout.strip()


# Worker scratch and gate artifacts that must never enter a diff or a commit. Default
# pathspec wildcards cross `/`, so `.aider*` also covers `.aider.tags.cache.v4/cache.db`
# -- `:(exclude,glob)` would not.
_SCRATCH = (
    ":(exclude).aider*",
    ":(exclude).zecor-scratch",
    ":(exclude)*.coverage*",
    ":(exclude)*__pycache__*",
    ":(exclude)*.pyc",
    ":(exclude)*.pytest_cache*",
    ":(exclude)*.ruff_cache*",
    ":(exclude)*.mypy_cache*",
    ":(exclude)htmlcov",
    ":(exclude).DS_Store",
    ":(exclude).zecor-sandbox.policy",  # written into the lane root by zecor.sandbox.wrap
)


@dataclass
class Lane:
    task_id: str
    repo_dir: Path
    path: Path
    branch: str
    base_sha: str = ""  # repo HEAD when the lane opened -- for the committed diff

    def committed_diff(self) -> str:
        """Everything this lane changed relative to where it branched -- works after
        `commit()` has cleared the working tree."""
        base = (
            self.base_sha
            or _git(["rev-list", "--max-parents=0", "HEAD"], cwd=self.path).split()[-1]
        )
        return _git(["diff", f"{base}..HEAD", "--", ".", *_SCRATCH], cwd=self.path)

    def reset(self) -> None:
        """Discard the working-tree changes; keep the branch."""
        _git(["checkout", "--", "."], cwd=self.path)
        _git(["clean", "-fd", "-e", ".aider*", "-e", ".zecor-scratch"], cwd=self.path)

    def commit(self, message: str, *, only: list[str] | None = None) -> str | None:
        """Commits the lane's change to its branch. `only` restricts the commit to the
        card's allow-list but falls back to everything-but-`_SCRATCH` when none of those
        paths exist, so a planner naming the wrong file can't sink a real fix. Returns
        None only when the worktree is genuinely unchanged."""
        real = [p for p in (only or []) if (self.path / p).exists()]
        if real:
            _git(["add", "--", *real], cwd=self.path)
        else:
            _git(["add", "--", ".", *_SCRATCH], cwd=self.path)
        if not _git(["diff", "--cached", "--name-only"], cwd=self.path).strip():
            return None
        _git(["commit", "-m", message], cwd=self.path)
        return _git(["rev-parse", "HEAD"], cwd=self.path)

    def diff(self) -> str:
        return _git(["diff", "--", ".", *_SCRATCH], cwd=self.path)

    def diff_hash(self) -> str:
        return hashlib.sha256(self.diff().encode()).hexdigest()[:16]

    def touched_files(self) -> frozenset[str]:
        out = _git(["diff", "--name-only", "--", ".", *_SCRATCH], cwd=self.path)
        tracked = _git(
            ["ls-files", "--others", "--exclude-standard", "--", ".", *_SCRATCH], cwd=self.path
        )
        return frozenset(f for f in (out + "\n" + tracked).splitlines() if f)

    def has_remote(self) -> bool:
        return bool(_git(["remote"], cwd=self.path).strip())

    def open_draft_pr(self, title: str, body: str) -> str | None:
        """Push the lane branch and open a DRAFT PR. Never merges or force-pushes; best
        effort -- a failure here does not un-land the task."""
        if not self.has_remote():
            return None
        _git(["push", "-u", "origin", self.branch], cwd=self.path)
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--head",
                self.branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=self.path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise LaneError(f"gh pr create failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def lines_changed(self) -> int:
        out = _git(["diff", "--numstat", "--", ".", *_SCRATCH], cwd=self.path)
        total = 0
        for line in out.splitlines():
            added, removed, *_ = (*line.split("\t"), "", "")
            for n in (added, removed):
                if n.isdigit():
                    total += int(n)
        return total


def open_lane(task_id: str, repo_dir: Path, base_ref: str = "HEAD") -> Lane:
    repo_dir = repo_dir.resolve()
    if not (repo_dir / ".git").exists():
        raise LaneError(f"{repo_dir} is not a git repo")
    LANES_DIR.mkdir(parents=True, exist_ok=True)
    lane_path = LANES_DIR / task_id
    branch = f"zecor/{task_id}"

    if lane_path.exists():
        close_lane(Lane(task_id, repo_dir, lane_path, branch), delete_branch=True)
    # A prior run may have left the branch behind even with the worktree gone.
    _git(["worktree", "prune"], cwd=repo_dir)
    with contextlib.suppress(LaneError):
        _git(["branch", "-D", branch], cwd=repo_dir)

    base_sha = _git(["rev-parse", base_ref], cwd=repo_dir)
    _git(["worktree", "add", "-b", branch, str(lane_path), base_ref], cwd=repo_dir)
    _install_pre_push(lane_path)
    return Lane(task_id, repo_dir, lane_path, branch, base_sha=base_sha)


def close_lane(lane: Lane, *, delete_branch: bool = False) -> None:
    try:
        _git(["worktree", "remove", "--force", str(lane.path)], cwd=lane.repo_dir)
    except LaneError:
        if lane.path.exists():
            shutil.rmtree(lane.path, ignore_errors=True)
        _git(["worktree", "prune"], cwd=lane.repo_dir)
    if delete_branch:
        with contextlib.suppress(LaneError):
            _git(["branch", "-D", lane.branch], cwd=lane.repo_dir)


def _install_pre_push(lane_path: Path) -> None:
    hooks = lane_path / ".git"
    # In a worktree, .git is a file pointing at the real gitdir.
    if hooks.is_file():
        gitdir_line = hooks.read_text().strip()
        real = gitdir_line.split("gitdir:", 1)[-1].strip()
        hook_dir = Path(real) / "hooks"
    else:
        hook_dir = hooks / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    dst = hook_dir / "pre-push"
    if _PRE_PUSH.exists():
        dst.write_text(_PRE_PUSH.read_text())
        dst.chmod(0o755)