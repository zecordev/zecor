# SPDX-License-Identifier: Apache-2.0
"""T0: deterministic ast-grep rule execution (build-queue item B)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from zecor import config, t0
from zecor.workers.base import Job
from zecor.workers.t0 import T0Worker

_HAVE_SG = shutil.which("ast-grep") is not None
pytestmark = pytest.mark.skipif(not _HAVE_SG, reason="ast-grep not installed")

_RULE = (
    "id: drop-not-implemented\n"
    "language: python\n"
    "# zecor: repos = demo, platform-*\n"
    "# zecor: intent = ~stub|placeholder|not implemented\n"
    "rule:\n  pattern: raise NotImplementedError\n"
    'fix: "pass"\n'
)


@pytest.fixture
def rules_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "rules"
    (d / "active").mkdir(parents=True)
    (d / "active" / "drop-not-implemented.yml").write_text(_RULE)
    monkeypatch.setattr(config, "RULES_DIR", d)
    return d


def test_parse_reads_the_fleet_header(rules_dir: Path) -> None:
    (rule,) = t0.load_active()
    assert rule.rule_id == "drop-not-implemented"
    assert rule.repos == ("demo", "platform-*")
    assert rule.intent_re == "stub|placeholder|not implemented"


def test_select_matches_repo_and_intent(rules_dir: Path) -> None:
    rules = t0.load_active()
    assert t0.select(rules, "demo", "remove the stub in auth")
    assert t0.select(rules, "platform-toolkit", "placeholder cleanup")
    assert not t0.select(rules, "demo", "fix a real bug")  # intent regex misses
    assert not t0.select(rules, "other-repo", "stub removal")  # repo glob misses


def test_apply_rewrites_a_match_and_reports_the_file(rules_dir: Path, tmp_path: Path) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=lane, check=True)
    (lane / "m.py").write_text("def f():\n    raise NotImplementedError\n")
    subprocess.run(["git", "add", "-A"], cwd=lane, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"],
        cwd=lane, check=True,
    )

    res = t0.apply(t0.load_active(), lane)
    assert res.changed == ("m.py",)
    assert "drop-not-implemented" in res.rules_run
    assert "pass" in (lane / "m.py").read_text()
    assert "NotImplementedError" not in (lane / "m.py").read_text()


def test_apply_is_a_noop_when_nothing_matches(rules_dir: Path, tmp_path: Path) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=lane, check=True)
    (lane / "m.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=lane, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"],
        cwd=lane, check=True,
    )
    res = t0.apply(t0.load_active(), lane)
    assert res.changed == ()


def test_worker_refuses_when_no_rule_matches(rules_dir: Path, tmp_path: Path) -> None:
    job = Job(
        prompt="", lane_dir=tmp_path, model=None, timeout_s=30.0, verify_cmd="true",
        repo="demo", intent="fix a real bug",  # intent regex misses
    )
    assert T0Worker().run(job).outcome == "refused"


def test_worker_passes_when_the_rewrite_makes_the_gate_green(
    rules_dir: Path, tmp_path: Path
) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=lane, check=True)
    (lane / "m.py").write_text("def f():\n    raise NotImplementedError\n")
    subprocess.run(["git", "add", "-A"], cwd=lane, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"],
        cwd=lane, check=True,
    )
    job = Job(
        prompt="", lane_dir=lane, model=None, timeout_s=30.0,
        verify_cmd="! grep -q NotImplementedError m.py",  # green once the rule fires
        repo="demo", intent="remove the stub",
    )
    res = T0Worker().run(job)
    assert res.outcome == "pass" and res.verify is not None and res.verify.ok
    assert "t0" in res.flags