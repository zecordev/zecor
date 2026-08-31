# SPDX-License-Identifier: Apache-2.0
"""Best-effort extraction of the failing-assertion set from a verify run.

The escalation triggers compare this set across attempts, so stability matters more
than exact ids -- the same break must produce the same set twice.
"""

from __future__ import annotations

import hashlib
import re

_PYTEST_FAIL = re.compile(r"^(FAILED|ERROR)\s+(\S+)", re.MULTILINE)
# vitest marks a failed test with U+00D7; escaped so the source stays ASCII.
_VITEST_FAIL = re.compile("^\\s*(?:\\u00d7|FAIL)\\s+(.+?)(?:\\s+\\d+ms)?$", re.MULTILINE)
_TSC_ERROR = re.compile(r"^(\S+\.tsx?)\((\d+),\d+\):\s+error", re.MULTILINE)


_PYTEST_PASS = re.compile(r"^(\S+::\S+)\s+PASSED", re.MULTILINE)
# vitest / jest check mark is U+2713; escaped to keep the source ASCII.
_VITEST_PASS = re.compile("^\\s*(?:✓|PASS)\\s+(.+?)(?:\\s+\\d+ms)?$", re.MULTILINE)


def passing_assertions(stdout: str, stderr: str) -> frozenset[str]:
    """Test ids the run reported PASSED -- the baseline green set for `new_breakage`.
    Only some runners enumerate passes (`pytest -v`, vitest reporters); an empty result
    just means `new_breakage` cannot fire, which is a safe degradation."""
    blob = f"{stdout}\n{stderr}"
    hits: set[str] = set()
    for m in _PYTEST_PASS.finditer(blob):
        hits.add(f"pytest:{m.group(1)}")
    for m in _VITEST_PASS.finditer(blob):
        hits.add(f"vitest:{m.group(1).strip()}")
    return frozenset(hits)


def failing_assertions(stdout: str, stderr: str, verify_exit: int) -> frozenset[str]:
    if verify_exit == 0:
        return frozenset()
    blob = f"{stdout}\n{stderr}"
    hits: set[str] = set()
    for m in _PYTEST_FAIL.finditer(blob):
        hits.add(f"pytest:{m.group(2)}")
    for m in _VITEST_FAIL.finditer(blob):
        hits.add(f"vitest:{m.group(1).strip()}")
    for m in _TSC_ERROR.finditer(blob):
        hits.add(f"tsc:{m.group(1)}:{m.group(2)}")
    if hits:
        return frozenset(hits)
    # Nothing recognised -- fold the tail of the output into one stable id so that an
    # unchanged failure still compares equal across attempts.
    tail = "\n".join(blob.strip().splitlines()[-12:])
    digest = hashlib.sha256(tail.encode()).hexdigest()[:12]
    return frozenset({f"exit{verify_exit}:{digest}"})