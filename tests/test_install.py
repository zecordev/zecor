# SPDX-License-Identifier: Apache-2.0
"""zecor.install: acts on zecor doctor gaps, dry-run by default."""

from __future__ import annotations

import pytest

from zecor import doctor, install


def _checks(*missing: str) -> list[doctor.Check]:
    names = ["jq", "rg", "git", "uv", "sqlite3", "tailscale", "mosh", "LM Studio",
             "  model foo", "T3 codex", "launchd units"]
    return [doctor.Check(n, n not in missing, "x", fix="codex login" if n.startswith("T3") else "")
            for n in names]


def test_dry_run_lists_steps_and_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "run_all", lambda: _checks("tailscale", "mosh"))
    text, code = install.run(do_it=False)
    assert code == 0 and "dry run" in text
    assert "brew install tailscale mosh" in text


def test_interactive_steps_are_printed_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "run_all", lambda: _checks("T3 codex", "launchd units"))
    steps = install.plan_steps()
    codex = next(s for s in steps if "codex" in s.label)
    assert codex.manual is True


def test_brew_pkg_names_map(monkeypatch: pytest.MonkeyPatch) -> None:
    assert install._brew_pkg_for("rg") == "ripgrep"
    assert install._brew_pkg_for("sg") == "ast-grep"
    assert install._brew_pkg_for("jq") == "jq"
    monkeypatch.setattr(doctor, "run_all", lambda: _checks("rg", "sqlite3"))
    brew = next(s for s in install.plan_steps() if s.label.startswith("brew install"))
    assert "ripgrep" in brew.argv and "sqlite" in brew.argv


def test_yes_runs_non_interactive_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "run_all", lambda: _checks("mosh"))  # one gap -> uv sync included
    calls: list[list[str]] = []

    class _P:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(install.shutil, "which", lambda _c: "/bin/x")
    monkeypatch.setattr(install.subprocess, "run", lambda argv, **kw: calls.append(argv) or _P())
    text, code = install.run(do_it=True)
    assert code == 0
    assert any(a[:2] == ["uv", "sync"] for a in calls)  # the always-on step ran