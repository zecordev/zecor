# SPDX-License-Identifier: Apache-2.0
"""The "CI green" primitive -- SHIP-IT.md step 2.

There is no GitHub Actions and no external CI: "CI green" means running the repo's
`.fleet/ci.sh` on this machine before anything merges or deploys -- a superset of the
gate (tests, plus for a deployable repo a `docker build` and container smoke test). A
repo with no `ci.sh` falls back to `.fleet/verify.sh`. Nothing here is wired into the
land path yet; this is the function the auto-merge gate will call once it is.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

CI_SCRIPT = ".zecor/ci.sh"
CI_SCRIPT_LEGACY = ".fleet/ci.sh"
GATE_SCRIPT = ".zecor/verify.sh"
GATE_SCRIPT_LEGACY = ".fleet/verify.sh"
DEFAULT_TIMEOUT_S = 1800.0


@dataclass(frozen=True)
class CIResult:
    ok: bool
    script: str  # which script ran, or "" if none
    exit_code: int
    reason: str  # one line -- empty on success


def _which(repo_dir: Path) -> str:
    for s in (CI_SCRIPT, CI_SCRIPT_LEGACY, GATE_SCRIPT, GATE_SCRIPT_LEGACY):
        if (repo_dir / s).exists():
            return s
    return ""


def run(repo_dir: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> CIResult:
    script = _which(repo_dir)
    if not script:
        return CIResult(False, "", -1, f"no {CI_SCRIPT} or {GATE_SCRIPT} in {repo_dir}")
    try:
        # invoke via the shell reading the file, so a script without +x still runs
        proc = subprocess.run(
            ["/bin/sh", script],
            cwd=repo_dir, capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return CIResult(False, script, -1, f"{script} timed out after {timeout_s:.0f}s")
    except OSError as e:
        return CIResult(False, script, -1, f"{script} could not run: {e}")
    rc = proc.returncode
    if rc == 0:
        return CIResult(True, script, 0, "")
    if rc in (126, 127):
        return CIResult(False, script, rc, f"{script} cannot execute (exit {rc})")
    last = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [f"exit {rc}"]
    return CIResult(False, script, rc, f"{script} exit {rc}: {last[0][:160]}")