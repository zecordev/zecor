# SPDX-License-Identifier: Apache-2.0
"""zecor.ci -- the 'CI green' primitive (SHIP-IT.md step 2)."""

from __future__ import annotations

import os
from pathlib import Path

from zecor import ci


def _script(repo: Path, rel: str, body: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


def test_prefers_ci_sh_over_verify_sh(tmp_path: Path) -> None:
    _script(tmp_path, ".zecor/verify.sh", "exit 1\n")  # would fail
    _script(tmp_path, ".zecor/ci.sh", "exit 0\n")  # wins
    r = ci.run(tmp_path)
    assert r.ok and r.script == ".zecor/ci.sh"


def test_falls_back_to_verify_sh(tmp_path: Path) -> None:
    _script(tmp_path, ".zecor/verify.sh", "exit 0\n")
    r = ci.run(tmp_path)
    assert r.ok and r.script == ".zecor/verify.sh"


def test_no_script_is_a_clean_failure(tmp_path: Path) -> None:
    r = ci.run(tmp_path)
    assert not r.ok and r.script == "" and "no .zecor/ci.sh" in r.reason


def test_reports_the_failing_tail(tmp_path: Path) -> None:
    _script(tmp_path, ".zecor/ci.sh", "echo 'boom: 3 tests failed' >&2\nexit 2\n")
    r = ci.run(tmp_path)
    assert not r.ok and r.exit_code == 2 and "boom: 3 tests failed" in r.reason


def test_a_non_executable_script_still_runs(tmp_path: Path) -> None:
    # ci.run invokes `/bin/sh <script>`, so a missing +x bit is not fatal
    p = tmp_path / ".zecor" / "ci.sh"
    p.parent.mkdir(parents=True)
    p.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(p, 0o644)
    assert ci.run(tmp_path).ok


def test_timeout(tmp_path: Path) -> None:
    _script(tmp_path, ".zecor/ci.sh", "sleep 5\n")
    r = ci.run(tmp_path, timeout_s=0.3)
    assert not r.ok and "timed out" in r.reason