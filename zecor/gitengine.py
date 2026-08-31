# SPDX-License-Identifier: Apache-2.0
"""In-process git via the `zecor-gitengine` Rust crate (gitoxide).

The engine makes the same handful of git queries thousands of times a night --
`rev-parse`, `merge-base`, ahead/behind vs. a base, the changed-file set. Each is a
fork+exec of `git`. This routes them through `zecor-gitengine` when it is built and
falls back to a `git` subprocess otherwise, so behaviour is identical either way.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from zecor import _rustbridge


@dataclass(frozen=True)
class BranchState:
    branch_sha: str
    base_sha: str
    merge_base: str | None
    ahead: int
    behind: int
    is_merged: bool
    is_descendant: bool


def _git(repo: Path, args: list[str]) -> str | None:
    out = _git_raw(repo, args)
    return out.strip() if out is not None else None


def _git_raw(repo: Path, args: list[str]) -> str | None:
    """Like `_git` but without the trailing/leading strip -- for output where leading
    whitespace is significant (`status --porcelain`)."""
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def rev_parse(repo: Path, rev: str = "HEAD") -> str | None:
    obj = _rustbridge.run_json("zecor-gitengine", ["rev-parse", rev, "--repo", str(repo)])
    if isinstance(obj, dict) and obj.get("sha"):
        return str(obj["sha"])
    return _git(repo, ["rev-parse", rev])


def merge_base(repo: Path, a: str, b: str) -> str | None:
    obj = _rustbridge.run_json(
        "zecor-gitengine", ["merge-base", a, b, "--repo", str(repo)]
    )
    if isinstance(obj, dict):
        return str(obj["sha"]) if obj.get("sha") else None
    return _git(repo, ["merge-base", a, b])


def branch_state(repo: Path, branch: str, base: str) -> BranchState | None:
    obj = _rustbridge.run_json(
        "zecor-gitengine", ["branch-state", branch, "--base", base, "--repo", str(repo)]
    )
    if isinstance(obj, dict) and "ahead" in obj:
        return BranchState(
            str(obj["branch_sha"]), str(obj["base_sha"]),
            (str(obj["merge_base"]) if obj.get("merge_base") else None),
            int(obj["ahead"]), int(obj["behind"]),
            bool(obj["is_merged"]), bool(obj["is_descendant"]),
        )
    # git fallback: two rev-list --count calls + merge-base
    a = _git(repo, ["rev-list", "--count", f"{base}..{branch}"])
    b = _git(repo, ["rev-list", "--count", f"{branch}..{base}"])
    bs = _git(repo, ["rev-parse", branch])
    base_s = _git(repo, ["rev-parse", base])
    if a is None or b is None or bs is None or base_s is None:
        return None
    ahead, behind = int(a), int(b)
    mb = _git(repo, ["merge-base", branch, base])
    return BranchState(bs, base_s, mb, ahead, behind, ahead == 0,
                       mb == base_s and behind == 0 and ahead > 0)


def changed_files(repo: Path, a: str, b: str) -> list[str]:
    obj = _rustbridge.run_json(
        "zecor-gitengine", ["changed-files", a, b, "--repo", str(repo)]
    )
    if isinstance(obj, dict) and isinstance(obj.get("files"), list):
        return [str(x) for x in obj["files"]]
    out = _git(repo, ["diff", "--name-only", f"{a}..{b}"])
    return out.splitlines() if out else []


@dataclass(frozen=True)
class LogEntry:
    sha: str
    summary: str
    author: str
    email: str
    time: int
    parents: list[str]


def log(repo: Path, rev: str = "HEAD", limit: int = 20) -> list[LogEntry]:
    obj = _rustbridge.run_json(
        "zecor-gitengine", ["log", rev, "--limit", str(limit), "--repo", str(repo)]
    )
    if isinstance(obj, list):
        return [
            LogEntry(str(d["sha"]), str(d["summary"]), str(d["author"]), str(d["email"]),
                     int(d["time"]), [str(p) for p in d.get("parents", [])])
            for d in obj if isinstance(d, dict)
        ]
    # \x01 as the field separator: unlike \x1f it is not whitespace, so `_git`'s
    # `.strip()` cannot eat a trailing empty field (the root commit's %P).
    fmt = "%H%x01%s%x01%an%x01%ae%x01%at%x01%P"
    out = _git(repo, ["log", f"-{limit}", f"--pretty={fmt}", rev])
    entries: list[LogEntry] = []
    for line in (out or "").splitlines():
        parts = line.split("\x01")
        if len(parts) == 6:
            entries.append(LogEntry(parts[0], parts[1], parts[2], parts[3], int(parts[4] or 0),
                                    parts[5].split() if parts[5] else []))
    return entries


@dataclass(frozen=True)
class StatusEntry:
    path: str
    change: str


@dataclass(frozen=True)
class Status:
    staged: list[StatusEntry]
    unstaged: list[StatusEntry]
    untracked: list[str]
    is_clean: bool


def status(repo: Path) -> Status | None:
    obj = _rustbridge.run_json("zecor-gitengine", ["status", "--repo", str(repo)])
    if isinstance(obj, dict) and "is_clean" in obj:
        def _es(rows: object) -> list[StatusEntry]:
            return [StatusEntry(str(r["path"]), str(r["change"]))
                    for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
        return Status(
            _es(obj.get("staged")), _es(obj.get("unstaged")),
            [str(x) for x in obj.get("untracked", [])], bool(obj["is_clean"]),
        )
    out = _git_raw(repo, ["status", "--porcelain=v1"])
    if out is None:
        return None
    staged: list[StatusEntry] = []
    unstaged: list[StatusEntry] = []
    untracked: list[str] = []
    _code = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "T": "type-change"}
    for line in out.splitlines():
        if len(line) < 3:
            continue
        x, y, path = line[0], line[1], line[3:]
        if x == "?" and y == "?":
            untracked.append(path)
            continue
        if x != " ":
            staged.append(StatusEntry(path, _code.get(x, "modified")))
        if y != " ":
            unstaged.append(StatusEntry(path, _code.get(y, "modified")))
    is_clean = not (staged or unstaged or untracked)
    return Status(staged, unstaged, untracked, is_clean)
