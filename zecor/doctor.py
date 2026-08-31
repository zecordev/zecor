# SPDX-License-Identifier: Apache-2.0
"""`zecor doctor` -- one report of every dependency, with the fix for each gap.

Read-only. Spends no tokens. Supersedes the scattered `--check` flags and folds in
`zecor check`'s tier probes plus the Tailscale / mosh pieces the newer campaigns need.
`zecor install` acts on what this flags.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from zecor import config

_BREW = {
    "jq": "brew install jq",
    "rg": "brew install ripgrep",
    "git": "brew install git",
    "gh": "brew install gh",
    "sg": "brew install ast-grep",
    "uv": "brew install uv",
    "sqlite3": "brew install sqlite",
}
_REMOTE = {
    "tailscale": "brew install tailscale && sudo tailscale up   (or: zecor remote setup)",
    "mosh": "brew install mosh",
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _run(argv: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, (p.stdout + p.stderr)
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def _tools() -> list[Check]:
    out: list[Check] = []
    for cmd, fix in {**_BREW, **_REMOTE}.items():
        p = _which(cmd)
        out.append(Check(cmd, p is not None, p or "not found", "" if p else fix))
    return out


def _local_models() -> list[Check]:
    from zecor import localmodel
    from zecor.config import (
        MODEL_DEEP_CODER,
        MODEL_EMBED,
        MODEL_PRIMARY_CODER,
        MODEL_REASONER,
        MODEL_ROUTER,
    )

    want = {MODEL_PRIMARY_CODER, MODEL_DEEP_CODER, MODEL_REASONER, MODEL_ROUTER, MODEL_EMBED}
    if not localmodel.available():
        return [Check("LM Studio", False, "not reachable on :1234",
                      "open LM Studio, then: lms server start --port 1234")]
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(f"{config.LM_BASE_URL}/models", timeout=5) as r:
            have = {m["id"] for m in json.loads(r.read()).get("data", [])}
    except Exception as e:
        return [Check("LM Studio", False, f"models query failed: {e}", "")]
    out = [Check("LM Studio", True, f"{len(have)} model(s) loaded")]
    for m in sorted(want):
        present = m in have
        out.append(Check(f"  model {m}", present, "loaded" if present else "missing",
                         "" if present else f"lms get {m}   (or load it in LM Studio)"))
    return out


def _vendors() -> list[Check]:
    checks = []
    for tier, cmd, probe, needle, fix in (
        ("T3 codex", "codex", ["codex", "login", "status"], "logged in", "codex login"),
        ("T4 claude", "claude", ["claude", "auth", "status"], "loggedin", "claude auth login"),
        ("T2b agy", "agy", ["agy", "models"], "gemini", "agy   (browser sign-in)"),
    ):
        if _which(cmd) is None:
            checks.append(Check(tier, False, "CLI not installed", fix))
            continue
        _rc, blob = _run(probe)
        ok = needle in blob.lower().replace(" ", "").replace('"', "")
        checks.append(Check(tier, ok, "authed" if ok else "installed but not authed",
                            "" if ok else fix))
    return checks


def _machine() -> list[Check]:
    out: list[Check] = []
    try:
        free_gb = shutil.disk_usage(os.path.expanduser("~")).free // (1024**3)
        ok = free_gb >= config.DISK_FLOOR_GB
        out.append(Check("disk free", ok, f"{free_gb}GB (floor {config.DISK_FLOOR_GB}GB)",
                         "" if ok else "free up space -- models + lanes want headroom"))
    except OSError:
        pass
    rc, mem = _run(["sysctl", "-n", "hw.memsize"])
    if rc == 0 and mem.strip().isdigit():
        gb = int(mem.strip()) // (1024**3)
        out.append(Check("RAM", gb >= 32, f"{gb}GB",
                         "" if gb >= 32 else "<32GB cannot host the local tier"))
    rc, wired = _run(["sysctl", "-n", "iogpu.wired_limit_mb"])
    if rc == 0:
        mb = wired.strip()
        out.append(Check("GPU wired limit", mb not in ("", "0"),
                         f"{mb}MB" if mb not in ("", "0") else "unset (default)",
                         "" if mb not in ("", "0") else "see setup/bootstrap.sh phase 4"))
    return out


def _launchd() -> list[Check]:
    la = os.path.expanduser("~/Library/LaunchAgents")
    nightly = os.path.exists(os.path.join(la, "local.fleet.plist"))
    cont = os.path.exists(os.path.join(la, "local.fleet.supervise.plist"))
    detail = ", ".join(
        [x for x, on in (("nightly", nightly), ("continuous", cont)) if on]
    ) or "none staged"
    return [Check("launchd units", nightly or cont, detail,
                  "" if (nightly or cont) else
                  "cp setup/local.fleet.plist ~/Library/LaunchAgents/ && launchctl load -w ...")]


def run_all() -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("python", True, sys.version.split()[0]))
    checks += _tools()
    checks += _local_models()
    checks += _vendors()
    checks += _machine()
    checks += _launchd()
    return checks


def report() -> tuple[str, int]:
    """(text, number-of-gaps)."""
    lines: list[str] = []
    gaps = 0
    for c in run_all():
        mark = "ok  " if c.ok else "MISS"
        lines.append(f"  {mark}  {c.name:22}  {c.detail}")
        if not c.ok and c.fix:
            lines.append(f"        -> {c.fix}")
        if not c.ok:
            gaps += 1
    lines.append("")
    lines.append(f"{gaps} gap(s)" + ("  -- run `zecor install`" if gaps else "  -- ready"))
    return "\n".join(lines), gaps