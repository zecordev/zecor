# SPDX-License-Identifier: Apache-2.0
"""Architecture legwork.

Architecture is not gateable, so Zecor does not decide it -- it only writes the
mechanical inventory as `digest/architecture-<topic>.md` for you to read and decide
yourself.
"""

from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def _run(cmd: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    return p.returncode, p.stdout + p.stderr


def _source_root(repo: Path) -> str:
    for c in ("src", "packages", "apps", "lib"):
        if (repo / c).is_dir():
            return c
    return "."


def _cycles(repo: Path, root: str) -> list[str]:
    if not shutil.which("madge"):
        return ["(madge not installed -- `npm i -g madge` for cycle detection)"]
    _rc, out = _run(
        ["madge", "--circular", "--extensions", "ts,tsx,js,jsx,mjs", root], repo, timeout=180
    )
    if "No circular dependency found" in out:
        return ["none"]
    lines = [ln.strip() for ln in out.splitlines() if "->" in ln]
    return lines[:40] or [out.strip()[:800] or "none"]


def _cross_package(repo: Path) -> list[str]:
    """In a monorepo, which workspace imports which."""
    pkgs = sorted(
        p.parent.name for base in ("packages", "apps") for p in repo.glob(f"{base}/*/package.json")
    )
    if not pkgs:
        return ["(not a packages/ or apps/ monorepo)"]
    edges: Counter[str] = Counter()
    _rc, out = _run(["rg", "-o", r"from ['\"]@[a-z0-9-]+/[a-z0-9-]+", "--no-filename", "-N"], repo)
    for line in out.splitlines():
        target = line.split("/")[-1].strip("'\"")
        if target in pkgs:
            edges[target] += 1
    return [f"{k}: imported {v} times" for k, v in edges.most_common(30)] or ["(no @scope imports)"]


def _churn(repo: Path, top: int = 20) -> list[str]:
    _rc, out = _run(
        ["git", "log", "--since=6.months", "--format=", "--name-only"], repo, timeout=60
    )
    counts = Counter(ln for ln in out.splitlines() if ln.strip())
    return [f"{n:4d}  {name}" for name, n in counts.most_common(top)]


def _sizes(repo: Path, root: str) -> list[str]:
    _rc, out = _run(["rg", "--files", "-t", "ts", "-t", "js", root], repo)
    files = [Path(f) for f in out.splitlines() if f.strip()]
    by_dir: Counter[str] = Counter()
    for f in files:
        by_dir[str(f.parent)] += 1
    return [f"{n:4d} files  {d}" for d, n in by_dir.most_common(20)]


def inventory(repo: Path, topic: str) -> str:
    root = _source_root(repo)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    blocks = [
        f"# architecture inventory: {topic}",
        f"repo: {repo}   source root: {root}/   generated: {now}",
        "",
        "This is legwork, not a recommendation. Read it, then have the design",
        "conversation with Opus yourself (plan ).",
        "",
        "## Circular dependencies (madge)",
        *(f"- {c}" for c in _cycles(repo, root)),
        "",
        "## Cross-package imports",
        *(f"- {e}" for e in _cross_package(repo)),
        "",
        "## Directory sizes",
        *(f"- {s}" for s in _sizes(repo, root)),
        "",
        "## Most-churned files (6 months)",
        *(f"- {c}" for c in _churn(repo)),
        "",
    ]
    return "\n".join(blocks) + "\n"


def write(repo: Path, topic: str, digest_dir: Path) -> Path:
    digest_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in topic).strip("-") or "repo"
    path = digest_dir / f"architecture-{safe}.md"
    path.write_text(inventory(repo, topic))
    return path