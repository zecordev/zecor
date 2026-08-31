# SPDX-License-Identifier: Apache-2.0
"""Resolve a card's `repo` name to a checkout on disk.

Convention: personal/business checkouts live under ~/code. Hilachem's are one level
deeper. Override with ZECOR_REPO_ROOTS (colon-separated) for anything else.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_ROOTS = (
    Path.home() / "code",
    Path.home() / "code" / "Hilachem",
)


class RepoNotFound(LookupError):
    pass


def repo_roots() -> list[Path]:
    extra = os.environ.get("ZECOR_REPO_ROOTS", "")
    roots = [Path(p).expanduser() for p in extra.split(":") if p]
    return [*roots, *_DEFAULT_ROOTS]


def resolve(repo: str) -> Path:
    for root in repo_roots():
        candidate = root / repo
        if (candidate / ".git").exists():
            return candidate.resolve()
    raise RepoNotFound(
        f"no git checkout named {repo!r} under {', '.join(str(r) for r in repo_roots())}"
    )


def gate_path(repo_dir: Path) -> Path | None:
    """The repo's gate script, if it has one. `.zecor/verify.sh` is the current
    convention; `.fleet/verify.sh` is still accepted for repos scaffolded before the
    rename."""
    for rel in (".zecor/verify.sh", ".fleet/verify.sh"):
        p = repo_dir / rel
        if p.exists():
            return p
    return None


def has_fleet_gate(repo_dir: Path) -> bool:
    p = gate_path(repo_dir)
    return p is not None and os.access(p, os.X_OK)