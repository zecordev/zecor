# SPDX-License-Identifier: Apache-2.0
"""zecor.doctor: dependency report (read-only, no tokens)."""

from __future__ import annotations

import pytest

from zecor import doctor


def test_report_returns_text_and_a_gap_count() -> None:
    text, gaps = doctor.report()
    assert isinstance(text, str) and "python" in text
    assert gaps >= 0
    assert ("gap(s)" in text)


def test_run_all_covers_the_expected_sections() -> None:
    names = {c.name for c in doctor.run_all()}
    assert "python" in names and "git" in names and "uv" in names
    assert any(n.startswith("T4") for n in names)  # a vendor tier
    assert "launchd units" in names


def test_a_missing_tool_carries_a_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_which", lambda cmd: None if cmd == "mosh" else "/usr/bin/x")
    mosh = next(c for c in doctor._tools() if c.name == "mosh")
    assert not mosh.ok and "brew install mosh" in mosh.fix