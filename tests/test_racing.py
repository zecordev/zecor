# SPDX-License-Identifier: Apache-2.0
"""racing: parallel approach racing (build-queue item H)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from zecor import config, racing
from zecor.cards import Card
from zecor.route import Route
from zecor.tiers import Tier
from zecor.workers.base import VerifyRun, WorkerResult


@pytest.fixture(autouse=True)
def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setattr(config, "ZECOR_ROOT", root)
    monkeypatch.setattr(config, "LANES_DIR", root / "lanes")
    (root / "lanes").mkdir(parents=True)
    return root


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=r, check=True)
    return r


def _card() -> Card:
    return Card("0042", "demo", "do the thing", "true", ["f.py"])


def _pass(lane: Path) -> WorkerResult:
    (lane / "f.py").write_text("x = 2  # fixed\n")
    return WorkerResult(outcome="pass", verify=VerifyRun(0, "", ""))


def _fail(_lane: Path) -> WorkerResult:
    return WorkerResult(outcome="fail", verify=VerifyRun(1, "", ""))


def test_first_green_approach_wins_and_losers_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZECOR_RACING", "1")
    repo = _repo(tmp_path)

    def disp(route: Route, job: object) -> WorkerResult:
        # the "minimal" approach (T3) fails; "refactor-first" (T4) passes
        return _pass(Path(job.lane_dir)) if route.tier is Tier.T4 else _fail(Path(job.lane_dir))  # type: ignore[attr-defined]

    res = racing.race(_card(), repo, dispatcher=disp)
    assert res.won and res.winner_label == "refactor-first"
    assert res.winner_branch == "zecor/0042-r1"
    # loser lane removed, winner lane kept with a commit
    assert not (config.LANES_DIR / "0042-r0").exists()
    body = subprocess.run(
        ["git", "log", "-1", "--format=%B", "zecor/0042-r1"],
        cwd=repo, capture_output=True, text=True,
    ).stdout
    assert "zecor race (refactor-first); task 0042" in body


def test_no_winner_closes_every_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_RACING", "1")
    repo = _repo(tmp_path)
    res = racing.race(_card(), repo, dispatcher=lambda r, j: _fail(Path(j.lane_dir)))
    assert not res.won
    assert not (config.LANES_DIR / "0042-r0").exists()
    assert not (config.LANES_DIR / "0042-r1").exists()


def test_a_racer_that_raises_does_not_sink_the_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZECOR_RACING", "1")
    repo = _repo(tmp_path)

    def disp(route: Route, job: object) -> WorkerResult:
        if route.tier is Tier.T3:
            raise RuntimeError("codex fell over")
        return _pass(Path(job.lane_dir))  # type: ignore[attr-defined]

    res = racing.race(_card(), repo, dispatcher=disp)
    assert res.won and res.winner_label == "refactor-first"


def test_faster_approach_wins_the_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_RACING", "1")
    repo = _repo(tmp_path)

    def disp(route: Route, job: object) -> WorkerResult:
        if route.tier is Tier.T4:
            time.sleep(0.5)  # the slow one also passes, but later
        return _pass(Path(job.lane_dir))  # type: ignore[attr-defined]

    res = racing.race(_card(), repo, dispatcher=disp)
    assert res.winner_label == "minimal"  # T3, no sleep


def test_can_race_respects_the_flag_and_the_governor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZECOR_RACING", raising=False)
    assert racing.can_race(4) is False
    monkeypatch.setenv("ZECOR_RACING", "1")
    assert racing.can_race(2) is True
    assert racing.can_race(1) is False  # governor says one lane