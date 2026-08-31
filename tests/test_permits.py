# SPDX-License-Identifier: Apache-2.0
"""Cross-process local-model permits."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from zecor import permits


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / ".permits"
    monkeypatch.setattr(permits, "PERMIT_DIR", d)
    monkeypatch.setattr(permits, "_REGISTRY", d / "local.json")
    monkeypatch.setattr(permits, "_LOCK", d / "local.lock")
    # keep the machine read out of the tests -- roomy box, no resident model sizes
    from zecor.resources import ResourceSnapshot

    monkeypatch.setattr(
        permits, "snapshot",
        lambda: ResourceSnapshot(
            ram_free_gb=40.0, ram_total_gb=64.0, swap_used_gb=0.0,
            load1=1.0, cpu_count=10, disk_free_gb=500.0,
        ),
    )


def test_light_kind_gets_a_noop_permit_immediately() -> None:
    p = permits.acquire("frontier_review")
    assert p is not None
    with p:
        pass  # no registry file written
    assert not (permits.PERMIT_DIR / "local.json").exists()


def test_local_permit_is_held_then_released() -> None:
    with permits.acquire("local_lane") as p:
        assert p is not None
        assert len(permits.held()) == 1
    assert permits.held() == []


def test_second_local_permit_blocks_and_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(permits, "LOCAL_MODEL_MAX", 1)
    with permits.acquire("local_lane"):
        # a different "thread key" -- simulate a second lane
        monkeypatch.setattr(permits, "_key", lambda: f"{os.getpid()}:99999")
        got = permits.acquire("local_lane", timeout_s=0.4, poll_s=0.1)
        assert got is None


def test_slot_frees_when_the_holder_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(permits, "LOCAL_MODEL_MAX", 1)
    first = permits.acquire("local_lane")
    assert first is not None
    monkeypatch.setattr(permits, "_key", lambda: f"{os.getpid()}:88888")
    assert permits.acquire("local_lane", timeout_s=0.2, poll_s=0.05) is None
    first.release()
    second = permits.acquire("local_lane", timeout_s=0.2, poll_s=0.05)
    assert second is not None
    second.release()


def test_dead_pid_entries_are_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(permits, "LOCAL_MODEL_MAX", 1)
    permits.PERMIT_DIR.mkdir(parents=True, exist_ok=True)
    # a registry left behind by a crashed process (pid very unlikely to be live)
    permits._REGISTRY.write_text(json.dumps({"999999:1": {"kind": "local_lane", "ts": 9e9}}))
    got = permits.acquire("local_lane", timeout_s=0.2, poll_s=0.05)
    assert got is not None  # the dead entry was reaped
    got.release()


def test_unknown_kind_is_gated_not_waved_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(permits, "LOCAL_MODEL_MAX", 1)
    with permits.acquire("local_lane"):
        monkeypatch.setattr(permits, "_key", lambda: f"{os.getpid()}:77777")
        # a typo'd / new GPU kind must not slip the cap
        assert permits.acquire("local_reveiw", timeout_s=0.2, poll_s=0.05) is None


def test_pid_reuse_is_caught_by_the_start_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(permits, "LOCAL_MODEL_MAX", 1)
    permits.PERMIT_DIR.mkdir(parents=True, exist_ok=True)
    # our own pid, but a start token that won't match -> holder is considered gone
    me = os.getpid()
    entry = {"kind": "local_lane", "ts": 9e18, "tok": "Thu Jan 1 00:00:00 1970"}
    permits._REGISTRY.write_text(json.dumps({f"{me}:1": entry}))
    got = permits.acquire("local_lane", timeout_s=0.2, poll_s=0.05)
    assert got is not None  # the mismatched-token entry was reaped despite a live pid
    got.release()


def test_two_small_model_jobs_can_run_together(monkeypatch: pytest.MonkeyPatch) -> None:
    from zecor.resources import ResourceSnapshot

    monkeypatch.setattr(
        permits, "snapshot",
        lambda: ResourceSnapshot(
            ram_free_gb=40.0, ram_total_gb=64.0, swap_used_gb=0.0,
            load1=1.0, cpu_count=10, disk_free_gb=500.0,
            lmstudio_sizes={"qwen3-4b-instruct-2507": 2.3},
        ),
    )
    monkeypatch.setattr(permits, "LOCAL_MODEL_MAX", 3)
    a = permits.acquire("local_model")
    monkeypatch.setattr(permits, "_key", lambda: f"{os.getpid()}:2")
    b = permits.acquire("local_model", timeout_s=0.3, poll_s=0.05)
    assert a is not None and b is not None  # two 2.3GB router jobs fit
    a.release()
    b.release()


def test_a_big_model_job_still_serialises(monkeypatch: pytest.MonkeyPatch) -> None:
    from zecor.resources import ResourceSnapshot

    monkeypatch.setattr(
        permits, "snapshot",
        lambda: ResourceSnapshot(
            ram_free_gb=40.0, ram_total_gb=64.0, swap_used_gb=0.0,
            load1=1.0, cpu_count=10, disk_free_gb=500.0,
            lmstudio_sizes={"qwen/qwen3-coder-30b": 17.2},
        ),
    )
    monkeypatch.setattr(permits, "LOCAL_MODEL_MAX", 3)
    with permits.acquire("local_lane"):
        monkeypatch.setattr(permits, "_key", lambda: f"{os.getpid()}:9")
        assert permits.acquire("local_lane", timeout_s=0.3, poll_s=0.05) is None