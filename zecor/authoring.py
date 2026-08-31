# SPDX-License-Identifier: Apache-2.0
"""Author the failing test first.

A feature request's suite is green at baseline, so without a red test first the
orchestrator would treat it as nothing to do.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from zecor.cards import Card
from zecor.lanes import Lane
from zecor.route import Route
from zecor.tiers import Tier
from zecor.workers import Job, WorkerResult
from zecor.workers.base import VerifyRun

_Dispatcher = Callable[[Route, Job], WorkerResult]
_Verifier = Callable[[Job], VerifyRun]

_FEATURE = re.compile(
    r"\b(add|implement|introduce|support|create|build|new|expose|enable)\b", re.I
)
_FIX = re.compile(
    r"\b(fix|bug|failing|regression|broken|crash|repair|incorrect|wrong|revert)\b", re.I
)
# a test/docs/chore ask is not a missing-runtime-behaviour feature -- don't demand a red test
_NOT_A_FEATURE = re.compile(
    r"\b(test|tests|testing|docs?|documentation|changelog|readme|comment|comments|"
    r"typo|rename|lint|format|whitespace|coverage)\b",
    re.I,
)
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|specs?)/"      # a tests/ or spec/ directory
    r"|(^|/)test_[^/]+$"                     # test_foo.py
    r"|(^|/)tests?\.[a-z0-9]+$"              # test.py / tests.py
    r"|_tests?\.[a-z0-9]+$"                  # foo_test.py / foo_tests.py
    r"|\.(spec|test)\.[a-z0-9]+$"            # Button.spec.tsx / thing.test.js
    r"|(^|/)conftest\.py$",
    re.I,
)
_CODE_EXT = frozenset(
    {"py", "js", "jsx", "ts", "tsx", "go", "rs", "java", "rb",
     "php", "c", "h", "cc", "cpp", "hpp", "cs", "kt", "swift", "scala"}
)


def wants_test_first(card: Card) -> bool:
    """A bounded/mechanical feature ask with no bug/test/docs language and at least
    one non-test file in scope -- there is no red test to inherit, so one must be
    written."""
    from zecor.task import Klass

    if card.klass not in (Klass.BOUNDED, Klass.MECHANICAL):
        return False
    intent = card.intent
    if not _FEATURE.search(intent) or _FIX.search(intent) or _NOT_A_FEATURE.search(intent):
        return False
    has_production_source = not card.allow_files or any(
        not _TEST_PATH.search(f) and f.rsplit(".", 1)[-1] in _CODE_EXT for f in card.allow_files
    )
    return has_production_source


_PROMPT = (
    "Write ONE failing test that will pass once this is done, and NOTHING ELSE:\n"
    "  {intent}\n\n"
    "Rules:\n"
    "- Touch only test files. Do NOT implement the feature or modify non-test source.\n"
    "- The test must FAIL now (the feature does not exist yet) and be specific:\n"
    "  assert the exact behaviour, not just that a name is importable.\n"
    "- Match the repo's existing test style and framework.\n"
    "Done means: `{verify}` now exits NON-zero because of your new test."
)


@dataclass
class AuthorResult:
    ok: bool
    note: str
    verify: VerifyRun | None = None


def author_failing_test(
    card: Card,
    lane: Lane,
    *,
    dispatcher: _Dispatcher,
    verifier: _Verifier,
    prefer: Tier = Tier.T1,
) -> AuthorResult:
    """One dispatch to write a failing test, then check the gate went red. On success
    the caller enters the normal loop; on failure the card goes to needs-human."""
    prompt = _PROMPT.format(intent=card.intent, verify=card.verify_cmd)
    job = Job(
        prompt=prompt,
        lane_dir=lane.path,
        model=None,
        timeout_s=600.0,
        verify_cmd=card.verify_cmd,
    )
    result = dispatcher(Route(prefer, reason="author the failing test first"), job)
    if result.outcome in ("refused", "timeout", "ratelimited"):
        return AuthorResult(False, f"test-authoring dispatch came back {result.outcome}")

    # a model asked for a failing test often implements the feature too; revert
    # non-test edits and proceed only if a real failing test survives
    reverted = _revert_non_test_edits(lane)
    vr = verifier(job)
    if vr.exit in (124, 126, 127):
        return AuthorResult(False, f"gate did not run cleanly after authoring (exit {vr.exit})", vr)
    touched = lane.touched_files()
    if not touched:
        note = "test-authoring wrote no test"
        if reverted:
            note += f" (reverted non-test edits to {', '.join(reverted)})"
        return AuthorResult(False, note, vr)
    if vr.ok:
        return AuthorResult(
            False, "the authored test does not fail -- it must assert the missing behaviour", vr
        )
    note = "failing test authored; the loop now owns making it pass"
    if reverted:
        note += f" (reverted an out-of-scope edit to {', '.join(reverted)})"
    return AuthorResult(True, note, vr)


def _revert_non_test_edits(lane: Lane) -> list[str]:
    """Undo any change the authoring step made outside test files; returns the
    reverted paths."""
    import subprocess

    reverted: list[str] = []
    for f in lane.touched_files():
        if _TEST_PATH.search(f):
            continue
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", f],
            cwd=lane.path, capture_output=True, text=True, check=False,
        ).returncode == 0
        if tracked:
            subprocess.run(["git", "checkout", "HEAD", "--", f], cwd=lane.path, check=False)
        else:
            (lane.path / f).unlink(missing_ok=True)
        reverted.append(f)
    return reverted


def is_test_path(path: str | Path) -> bool:
    return bool(_TEST_PATH.search(str(path)))