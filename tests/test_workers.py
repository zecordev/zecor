# SPDX-License-Identifier: Apache-2.0
"""Worker wiring + plumbing tests. No network, no vendor CLIs."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from zecor.route import Route
from zecor.tiers import Tier
from zecor.workers import NoWorkerForTier, dispatch, worker_for
from zecor.workers.antigravity import AntigravityWorker
from zecor.workers.base import Job, run_capped
from zecor.workers.claude_code import ClaudeWorker
from zecor.workers.codex import CodexWorker
from zecor.workers.gemini import GeminiWorker
from zecor.workers.local import LocalWorker
from zecor.workers.luna import LunaWorker


@pytest.mark.parametrize(
    ("tier", "cls"),
    [
        (Tier.T1, LocalWorker),
        (Tier.T2A, GeminiWorker),
        (Tier.T2B, AntigravityWorker),
        (Tier.T3, CodexWorker),
        (Tier.T4, ClaudeWorker),
        (Tier.T5, LunaWorker),
    ],
)
def test_worker_for_maps_each_tier(tier: Tier, cls: type) -> None:
    assert isinstance(worker_for(tier), cls)


@pytest.mark.parametrize("tier", [Tier.NEEDS_HUMAN, Tier.DEFER])
def test_no_worker_for_control_tiers(tier: Tier) -> None:
    with pytest.raises(NoWorkerForTier):
        worker_for(tier)


def test_t0_has_a_worker_now() -> None:
    from zecor.workers.t0 import T0Worker

    assert isinstance(worker_for(Tier.T0), T0Worker)


def _job(tmp_path: Path) -> Job:
    return Job(prompt="x", lane_dir=tmp_path, model=None, timeout_s=2.0, verify_cmd="true")


def test_http_workers_refuse_without_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_METERED_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert dispatch(Route(Tier.T2A), _job(tmp_path)).outcome == "refused"
    assert dispatch(Route(Tier.T5), _job(tmp_path)).outcome == "refused"


def test_run_capped_kills_on_timeout(tmp_path: Path) -> None:
    t0 = time.time()
    rc, _out, _err, killed = run_capped(["/bin/sh", "-c", "sleep 30"], cwd=tmp_path, timeout_s=1.0)
    assert killed is True
    assert rc == 124
    assert time.time() - t0 < 25  # killed, not waited out (sleep 30); loose for loaded CI


def test_run_capped_returns_output(tmp_path: Path) -> None:
    rc, out, _err, killed = run_capped(["/bin/sh", "-c", "echo hello"], cwd=tmp_path, timeout_s=5.0)
    assert rc == 0
    assert killed is False
    assert out.strip() == "hello"


def test_run_capped_strips_provider_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    rc, out, _err, _killed = run_capped(
        ["/bin/sh", "-c", "echo ${ANTHROPIC_API_KEY:-GONE}"], cwd=tmp_path, timeout_s=5.0
    )
    assert out.strip() == "GONE"