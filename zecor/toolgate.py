# SPDX-License-Identifier: Apache-2.0
"""Command allow-list for `hardened` mode: every subprocess argv is checked here before
it runs, by basename against a small set of allowed subcommands, with a handful of
dangerous options rejected outright. `guarded` and `greenfield` are unrestricted (they
run in an isolated workspace or on a repo with no production users). Does not defend
against an attacker who can already place an executable earlier on `PATH` --
`workers.base.run_capped` pins a trusted `PATH` in hardened mode to narrow that."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from zecor import modes

# The canonical deterministic-gate script, any common spelling -- `sh -c <X>` is allowed
# only when X reduces to one of these.
_CANON_GATE = (
    ".zecor/verify.sh", "./.zecor/verify.sh", ".zecor/ci.sh", "./.zecor/ci.sh",
    ".fleet/verify.sh", "./.fleet/verify.sh", ".fleet/ci.sh", "./.fleet/ci.sh",
)

# basename -> allowed leading subcommand tuples. "*" is deliberately rare.
_ALLOW: dict[str, tuple[tuple[str, ...], ...]] = {
    "git": (
        ("status",), ("diff",), ("log",), ("show",), ("branch",), ("rev-parse",),
        ("ls-files",), ("blame",), ("merge-base",), ("cat-file",), ("worktree",),
        # branch-local writes -- needed to build the branch a draft PR points at
        ("add",), ("commit",), ("checkout",), ("switch",), ("stash",),
        ("notes",), ("fetch",), ("fetch", "origin"), ("config", "--get"),
    ),
    "python": (("-m", "pytest"), ("-m", "mypy"), ("-m", "ruff"), ("-m", "build"),
               ("-m", "vitest"), ("-V",)),
    "python3": (("-m", "pytest"), ("-m", "mypy"), ("-m", "ruff"), ("-V",)),
    "pytest": (("*",),),
    "mypy": (("*",),),
    "ruff": (("check",), ("format",), ("--version",)),
    "uv": (("run",), ("sync",), ("pip", "list"), ("--version",)),
    "npm": (("test",), ("run", "test"), ("run", "build"), ("run", "typecheck"),
            ("ci",), ("install",), ("--version",)),
    "pnpm": (("test",), ("run", "test"), ("run", "build"), ("install",), ("-v",)),
    "node": (("--version",),),
    "cargo": (("check",), ("test",), ("build",), ("clippy",)),
    "go": (("test",), ("build",), ("vet",)),
    "tsc": (("*",),),
    "vitest": (("run",), ("--version",)),
    "ast-grep": (("*",),),
    "sg": (("*",),),
    "rg": (("*",),),
    "pgrep": (("*",),),
    "sysctl": (("-n",),),
    "vm_stat": (("*",),),
    "df": (("*",), ("-g",)),
    "aider": (("*",),),
    "claude": (("*",),),
    "codex": (("*",),),
    "agy": (("*",),),
    "lms": (("*",),),
    "caffeinate": (("*",),),
    # the isolation wrapper -- `run`'s wrapped argv, after `--`, is itself checked below.
    "zecor-sandbox": (("run",), ("show",)),
}

_DANGEROUS_OPTS = frozenset({"-c", "--exec", "-e", "--eval", "--upload-pack", "--receive-pack"})


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def _basename(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def _safe_gate_command(arg: str) -> bool:
    """Safe if the script is the canonical gate, or parses to a single command whose
    argv is itself allow-listed (a narrow `pytest tests/x.py` gate, say); anything with
    a shell operator (`;`, `|`, `&&`, `$(`, backticks) is rejected."""
    s = arg.strip()
    for pre in ("bash ", "sh ", "/bin/sh ", "/bin/bash ", "uv run ", "./"):
        if s.startswith(pre):
            s = s[len(pre) :]
    s = s.strip()
    if s.rstrip("/") in _CANON_GATE or s in _CANON_GATE:
        return True
    if any(op in arg for op in (";", "|", "&", "$(", "`", "\n", ">", "<", "&&", "||")):
        return False
    try:
        return check(shlex.split(arg)).allowed
    except ValueError:
        return False


def check(argv: list[str]) -> Decision:
    """Is this argv allowed to run in the current mode?"""
    if not modes.shell_is_allow_listed():
        return Decision(True, f"{modes.current_mode().value}: shell not restricted")
    if not argv:
        return Decision(False, "empty argv")

    base = _basename(argv[0])
    rest = tuple(argv[1:])

    # the wrapper is trusted, but the argv it runs is not -- check it as if unwrapped.
    if base == "zecor-sandbox":
        if rest[:1] == ("show",):
            return Decision(True, "zecor-sandbox show")
        if rest[:1] == ("run",):
            if "--" not in rest:
                return Decision(False, "zecor-sandbox run: expected `-- CMD`")
            inner = list(rest[rest.index("--") + 1 :])
            if not inner:
                return Decision(False, "zecor-sandbox run: no command after `--`")
            return check(inner)
        return Decision(False, f"zecor-sandbox: subcommand not allowed ({' '.join(rest[:2])})")

    if base in ("sh", "bash") and rest[:1] == ("-c",):
        if len(rest) >= 2 and _safe_gate_command(rest[1]):
            return Decision(True, f"{base} -c <safe gate command>")
        return Decision(False, f"{base} -c: not a canonical gate or allow-listed command")

    rules = _ALLOW.get(base)
    if rules is None:
        return Decision(False, f"binary not on the hardened allow-list: {base!r}")

    bad = _DANGEROUS_OPTS.intersection(rest)
    if bad and base not in ("git",):  # git's -e/-c collisions are handled by subcommand
        return Decision(False, f"{base}: dangerous option {sorted(bad)[0]!r}")
    if base in ("python", "python3") and rest[:1] == ("-c",):
        return Decision(False, "python -c is not allowed in hardened mode")
    # git fetch with an explicit URL reaches the network from a lane -- block it
    if base == "git" and rest[:1] == ("fetch",) and len(rest) > 1 and "://" in rest[1]:
        return Decision(False, "git fetch <url> not allowed; use `git fetch origin`")

    for allowed in rules:
        if allowed == ("*",):
            return Decision(True, f"{base}: any args allowed")
        if rest[: len(allowed)] == allowed:
            return Decision(True, f"{base} {' '.join(allowed)}")
    return Decision(False, f"{base}: args not allow-listed ({' '.join(rest[:3])} …)")


def check_line(command: str) -> Decision:
    try:
        return check(shlex.split(command))
    except ValueError:
        return Decision(False, "unparseable command line")
