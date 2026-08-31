# SPDX-License-Identifier: Apache-2.0
"""core.leases: SQLite dispatch leases (Command Center)."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zecor import config
from zecor.core import leases as lz


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "db" / "zecor.db"
    monkeypatch.setattr(config, "DB_PATH", p)
    return p


def _free_pid() -> int:
    for pid in range(999_000, 990_000, -1):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            continue
    raise RuntimeError("no free pid")


def _stamp(task_id: str, pid: int, expires: str, token: str = "x") -> None:
    db = lz._db()
    db.execute(
        "INSERT INTO lease (task_id, token, pid, expires, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(task_id) DO UPDATE SET token=excluded.token, pid=excluded.pid, "
        "expires=excluded.expires",
        (task_id, token, pid, expires, "now"),
    )
    db.commit()
    db.close()


def test_claim_refresh_release_cycle() -> None:
    lz.claim("0001", "tok-a")
    row = lz.get("0001")
    assert row is not None and row.token == "tok-a" and row.pid == os.getpid()
    assert not lz.is_zombie(row)
    lz.refresh("0001", "tok-a")
    lz.release("0001", "tok-a")
    assert lz.get("0001") is None


def test_two_claims_same_process_cannot_both_hold_it() -> None:
    lz.claim("0001", "thread-a")
    with pytest.raises(lz.Leased):
        lz.claim("0001", "thread-b")


def test_claim_reaps_a_dead_lease() -> None:
    _stamp("0001", _free_pid(), "2099-01-01T00:00:00+00:00", token="old")
    lz.claim("0001", "new")  # dead holder, far-future expiry -> still reaped
    assert lz.get("0001").token == "new"


def test_claim_refuses_a_live_foreign_lease() -> None:
    _stamp("0001", os.getpid(), "2099-01-01T00:00:00+00:00", token="theirs")
    with pytest.raises(lz.Leased):
        lz.claim("0001", "mine")


def test_refresh_raises_after_clear() -> None:
    lz.claim("0001", "mine")
    lz.clear("0001")
    with pytest.raises(lz.LeaseLost):
        lz.refresh("0001", "mine")


def test_is_zombie_dead_holder_now_live_holder_grace() -> None:
    _stamp("0001", _free_pid(), "2000-01-01T00:00:00+00:00")
    assert lz.is_zombie(lz.get("0001")) is True  # dead pid -> at once
    past = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    _stamp("0002", os.getpid(), past)
    assert lz.is_zombie(lz.get("0002")) is False  # live holder, within grace
    assert lz.is_zombie(lz.get("0002"), grace_s=1) is True


def test_200_concurrent_claims_yield_exactly_one_winner() -> None:
    results: list[str] = []

    def go(n: int) -> None:
        try:
            lz.claim("0001", f"t{n}")
            results.append("won")
        except lz.Leased:
            results.append("lost")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(go, range(200)))
    assert results.count("won") == 1
    assert results.count("lost") == 199


def test_clear_if_is_token_conditional() -> None:
    lz.claim("0001", "A")
    assert lz.clear_if("0001", "stale-B") is False  # not the current token -> untouched
    assert lz.get("0001") is not None
    assert lz.clear_if("0001", "A") is True
    assert lz.get("0001") is None


def test_lease_context_manager_holds_and_releases() -> None:
    with lz.lease("0001", interval_s=0.05) as lost:
        assert lz.get("0001") is not None
        assert lost() is False
    assert lz.get("0001") is None


def test_lease_context_reports_a_cleared_lease() -> None:
    with lz.lease("0001", interval_s=0.05) as lost:
        lz.clear("0001")
        deadline = time.time() + 2
        while not lost() and time.time() < deadline:
            time.sleep(0.05)
        assert lost() is True


def test_shutdown_releases_our_leases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from zecor import cli, config
    from zecor.core import leases as _lz

    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "STOP")
    _stamp("0001", os.getpid(), _lz._iso_in(300), token="t1")
    _stamp("0002", os.getpid(), _lz._iso_in(300), token="t2")
    cli.note_lease("0001")
    cli.note_lease("0002")
    assert set(_lz.active()) == {"0001", "0002"}
    cli._SHUTTING_DOWN = False
    with pytest.raises(SystemExit):
        cli._shutdown(15)
    assert _lz.active() == {} and config.STOP_FILE.exists()