# SPDX-License-Identifier: Apache-2.0
"""failing_assertions() -- stable set extraction across attempts."""

from __future__ import annotations

from zecor.failures import failing_assertions


def test_green_is_empty() -> None:
    assert failing_assertions("all good", "", 0) == frozenset()


def test_pytest_failures() -> None:
    out = "FAILED tests/test_x.py::test_a - assert 1 == 2\nFAILED tests/test_x.py::test_b\n"
    fa = failing_assertions(out, "", 1)
    assert fa == frozenset({"pytest:tests/test_x.py::test_a", "pytest:tests/test_x.py::test_b"})


def test_tsc_errors() -> None:
    err = "src/router/resolve.ts(142,7): error TS2532: Object is possibly 'undefined'.\n"
    assert failing_assertions("", err, 2) == frozenset({"tsc:src/router/resolve.ts:142"})


def test_unrecognised_output_is_stable_across_identical_runs() -> None:
    blob = "some\nopaque\nbuild failure\nwith no known markers\n"
    a = failing_assertions(blob, "", 1)
    b = failing_assertions(blob, "", 1)
    assert a == b
    assert len(a) == 1


def test_unrecognised_output_differs_when_the_failure_changes() -> None:
    a = failing_assertions("opaque failure ONE", "", 1)
    b = failing_assertions("opaque failure TWO", "", 1)
    assert a != b