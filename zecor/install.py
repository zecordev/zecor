# SPDX-License-Identifier: Apache-2.0
"""`zecor install` -- act on what `zecor doctor` flags.

Idempotent and resumable, and safe by default -- it prints the plan; `--yes` runs it.
It never runs an interactive sign-in; those are printed for you to do.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from zecor import config, doctor


@dataclass(frozen=True)
class Step:
    label: str
    argv: list[str]
    manual: bool = False  # printed, not run (interactive)
    in_repo: bool = False  # run with cwd = Zecor repo, not wherever the user is


def _brew_pkg_for(name: str) -> str:
    return {"rg": "ripgrep", "sg": "ast-grep", "sqlite3": "sqlite"}.get(name, name)


def plan_steps() -> list[Step]:
    """Derive the install steps from a fresh `zecor doctor` run."""
    checks = doctor.run_all()
    steps: list[Step] = []
    brew_missing: list[str] = []
    for c in checks:
        if c.ok:
            continue
        n = c.name
        if n in {"jq", "rg", "git", "gh", "sg", "uv", "sqlite3", "tailscale", "mosh"}:
            brew_missing.append(_brew_pkg_for(n))
        elif n == "LM Studio":
            steps.append(Step("start LM Studio + load a model", ["lms", "server", "start",
                                                                  "--port", "1234"], manual=True))
        elif n.startswith("  model "):
            steps.append(Step(f"pull {n.strip()[6:]}", ["lms", "get", n.strip()[6:]]))
        elif n.startswith(("T3", "T4", "T2b")):
            steps.append(Step(f"{n}: sign in", (c.fix or "").split(), manual=True))
        elif n == "launchd units":
            steps.append(Step(
                "stage the nightly launchd unit",
                ["cp", str(config.REPO_ROOT / "setup" / "local.fleet.plist"),
                 f"{_home()}/Library/LaunchAgents/"], manual=True))
    if brew_missing:
        steps.insert(0, Step(f"brew install {' '.join(brew_missing)}",
                             ["brew", "install", *brew_missing]))
    # only re-sync the venv if something about the toolchain actually changed; a green
    # doctor means the environment is already in place and `uv sync` would be a no-op
    # at best and prune extras at worst.
    if any(not c.ok for c in checks):
        steps.append(Step("uv sync (dev + console groups)",
                          ["uv", "sync", "--group", "dev", "--group", "console"],
                          in_repo=True))
    return steps


def _home() -> str:
    import os

    return os.path.expanduser("~")


def run(*, do_it: bool) -> tuple[str, int]:
    steps = plan_steps()
    lines = ["install plan:"]
    ran = failed = 0
    for s in steps:
        if s.manual:
            lines.append(f"  (you)  {s.label}")
            lines.append(f"         {' '.join(s.argv)}")
            continue
        if not do_it:
            lines.append(f"  [ ]    {s.label}")
            continue
        if s.argv and shutil.which(s.argv[0]) is None:
            lines.append(f"  SKIP   {s.label}  ({s.argv[0]} not on PATH -- install it first)")
            failed += 1
            continue
        lines.append(f"  ...    {s.label}")
        cwd = str(config.REPO_ROOT) if s.in_repo else None
        try:
            p = subprocess.run(s.argv, capture_output=True, text=True, timeout=1200,
                               check=False, cwd=cwd)
        except (OSError, subprocess.SubprocessError) as e:
            lines.append(f"  FAIL   {e}")
            failed += 1
            continue
        if p.returncode == 0:
            lines.append("  ok")
            ran += 1
        else:
            lines.append(f"  FAIL   exit {p.returncode}: {(p.stderr or p.stdout).strip()[-300:]}")
            failed += 1
    if not do_it:
        lines.append("\n(dry run -- pass --yes to execute the [ ] steps; (you) steps stay manual)")
        return "\n".join(lines), 0
    lines.append(f"\n{ran} step(s) done, {failed} failed. Re-run `zecor doctor` to confirm.")
    return "\n".join(lines), 1 if failed else 0