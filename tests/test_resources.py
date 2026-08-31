# SPDX-License-Identifier: Apache-2.0
"""The resource governor. Decisions over a synthetic snapshot, plus one
smoke test that the real probe runs."""

from __future__ import annotations

import os

import pytest

from zecor import resources
from zecor.resources import ResourceSnapshot, advise_lanes, can_start, snapshot


def snap(**kw: object) -> ResourceSnapshot:
    base: dict[str, object] = dict(
        ram_free_gb=20.0,
        ram_total_gb=64.0,
        swap_used_gb=0.5,
        load1=2.0,
        cpu_count=10,
        disk_free_gb=500.0,
        aider_jobs=0,
        zecor_runs=0,
    )
    # allow tests to say local_consumers=N as shorthand for N big-model permit holders
    if "local_consumers" in kw:
        n = int(kw.pop("local_consumers"))  # type: ignore[call-overload]
        base["aider_jobs"] = n
        base["permit_holders"] = n
        base["permit_gbs"] = [18.0] * n
    base.update(kw)
    return ResourceSnapshot(**base)  # type: ignore[arg-type]


def test_local_work_blocked_when_a_big_consumer_is_running() -> None:
    ok, why = can_start("local_lane", snap=snap(permit_gbs=[18.0]))
    assert not ok and "big-model" in why


def test_a_small_model_job_may_start_alongside_a_big_one() -> None:
    ok, _ = can_start("local_model", snap=snap(permit_gbs=[18.0], ram_free_gb=20.0, load1=1.0))
    assert ok  # the 4B router tier isn't blocked by a big-model consumer


def test_local_work_ok_when_idle_and_roomy() -> None:
    ok, _ = can_start("local_lane", snap=snap(local_consumers=0, ram_free_gb=20.0, load1=1.0))
    assert ok


def test_local_work_blocked_under_the_ram_floor() -> None:
    ok, why = can_start("local_model", snap=snap(ram_free_gb=3.0))
    assert not ok and "floor" in why


def test_light_work_survives_a_busy_gpu() -> None:
    # a cloud-backed review does not touch LM Studio -- a local consumer must not block it
    ok, _ = can_start("frontier_review", snap=snap(local_consumers=2, ram_free_gb=8.0))
    assert ok


def test_light_work_blocked_only_when_ram_is_truly_gone() -> None:
    assert not can_start("frontier_review", snap=snap(ram_free_gb=1.0))[0]


def test_swap_thrash_blocks_everything() -> None:
    assert not can_start("frontier_review", snap=snap(swap_used_gb=9.0))[0]
    assert not can_start("local_lane", snap=snap(swap_used_gb=9.0))[0]


def test_runaway_load_blocks_everything() -> None:
    assert not can_start("test_run", snap=snap(load1=40.0, cpu_count=10))[0]


def test_unknown_kind_is_treated_as_light() -> None:
    assert can_start("something_new", snap=snap(local_consumers=3))[0]


def test_advise_lanes_drops_to_one_when_tight() -> None:
    assert advise_lanes(snap=snap(ram_free_gb=5.0)) == 1
    assert advise_lanes(snap=snap(load1=9.5, cpu_count=10)) == 1
    assert advise_lanes(snap=snap(swap_used_gb=9.0)) == 1


def test_advise_lanes_allows_two_only_with_headroom() -> None:
    assert advise_lanes(snap=snap(ram_free_gb=40.0, load1=1.0, cpu_count=10)) == 2
    assert advise_lanes(snap=snap(ram_free_gb=10.0, load1=1.0, cpu_count=10)) == 1


def test_advise_lanes_holds_at_one_while_an_aider_runs() -> None:
    # a second lane = a second aider on the same resident model -- never while one runs
    assert advise_lanes(snap=snap(ram_free_gb=40.0, load1=1.0, cpu_count=10, aider_jobs=1)) == 1


def test_env_nan_does_not_kill_the_module() -> None:
    # regression: int(float("nan")) at import raised and took the whole CLI down
    import importlib

    import zecor.resources as rr

    for bad in ("nan", "inf", "-inf", "garbage"):
        for var in ("ZECOR_MAX_LANES", "ZECOR_LOCAL_MODEL_MAX", "ZECOR_LOAD_FACTOR"):
            os.environ[var] = bad
    try:
        importlib.reload(rr)
        assert rr.MAX_LANES >= 1 and rr.LOCAL_MODEL_MAX >= 1
        assert rr.LOAD_FACTOR == 0.9  # nan rejected -> default
    finally:
        for var in ("ZECOR_MAX_LANES", "ZECOR_LOCAL_MODEL_MAX", "ZECOR_LOAD_FACTOR"):
            os.environ.pop(var, None)
        importlib.reload(rr)


def test_snapshot_runs_on_the_real_machine() -> None:
    s = snapshot()
    assert s.ram_total_gb > 0
    assert s.cpu_count >= 1
    assert isinstance(s.as_dict(), dict)


def test_probe_failure_degrades_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "_run", lambda *a, **k: "")  # every probe returns nothing
    s = snapshot()
    # mem probe fell back to a pessimistic guess; nothing raised
    assert s.probe_errors
    assert isinstance(can_start("local_lane", snap=s), tuple)


def test_a_hung_probe_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # regression: subprocess.TimeoutExpired is not an OSError -- a hung `lms`/`sysctl`
    # used to crash snapshot() and take `zecor run` with it.
    import subprocess

    def boom(*a: object, **k: object) -> object:
        raise subprocess.TimeoutExpired(cmd="probe", timeout=1.0)

    monkeypatch.setattr(resources.subprocess, "run", boom)
    assert resources._run(["sysctl", "-n", "hw.memsize"]) == ""
    s = snapshot()  # must not raise
    assert s.probe_errors