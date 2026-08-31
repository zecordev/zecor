# SPDX-License-Identifier: Apache-2.0
"""core.intents: the verbs (Command Center build step 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import config
from zecor.cards import Card
from zecor.core import cards as core_cards
from zecor.core import intents


@pytest.fixture(autouse=True)
def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "zecor"
    for name, sub in [
        ("ZECOR_ROOT", ""), ("TASKS_DIR", "tasks"), ("LOGS_DIR", "logs"),
        ("QUEUE_INBOX", "queue/inbox.md"), ("DB_PATH", "db/zecor.db"),
        ("STOP_FILE", "STOP"),
    ]:
        monkeypatch.setattr(config, name, root / sub if sub else root)
    (root / "tasks").mkdir(parents=True)
    return root


def test_stop_and_go_toggle_the_flag() -> None:
    assert intents.is_stopped() is False
    assert intents.stop().ok
    assert intents.is_stopped() is True
    assert intents.go().ok
    assert intents.is_stopped() is False
    assert intents.go().ok  # idempotent


def test_retry_requeues_a_blocked_card() -> None:
    c = Card("0001", "demo", "x", "./v.sh", ["a.py"])
    c.state = "blocked"
    c.write(config.TASKS_DIR)
    res = intents.retry("0001", tier="t4", model="opus", note="try harder")
    assert res.ok and res.data["retry_count"] == 1
    got = core_cards.read("0001")
    assert got.state == "ready" and got.retry_tier == "t4" and got.retry_context == "try harder"


def test_retry_declines_a_landed_card() -> None:
    c = Card("0002", "demo", "x", "./v.sh", ["a.py"])
    c.state = "landed"
    c.write(config.TASKS_DIR)
    res = intents.retry("0002")
    assert not res.ok and "nothing to retry" in res.message


def test_retry_declines_a_live_leased_card() -> None:
    from zecor.core import leases

    c = Card("0003", "demo", "x", "./v.sh", ["a.py"])
    c.state = "blocked"
    c.write(config.TASKS_DIR)
    leases.claim("0003", "live-run")  # this pid, far future -> a live lease
    res = intents.retry("0003")
    assert not res.ok and "holds it" in res.message


def test_retry_missing_card() -> None:
    assert intents.retry("9999").ok is False


def test_assign_move_sets_and_clears() -> None:
    c = Card("0007", "demo", "x", "./v.sh", ["a.py"])
    c.write(config.TASKS_DIR)
    assert intents.assign_move("0007", "Deep-Review").ok
    assert core_cards.read("0007").move == "deep-review"
    assert intents.assign_move("0007", "  ").ok
    assert core_cards.read("0007").move is None
    assert intents.assign_move("9999", "x").ok is False


def test_clear_zombie() -> None:
    from zecor.core import leases

    Card("0004", "demo", "x", "./v.sh", ["a.py"]).write(config.TASKS_DIR)
    leases.claim("0004", "dead-run")
    assert intents.clear_zombie("0004").ok
    assert leases.get("0004") is None


def test_retry_does_not_clobber_a_lease_reclaimed_mid_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zecor.core import leases

    c = Card("0005", "demo", "x", "./v.sh", ["a.py"])
    c.state = "blocked"
    c.write(config.TASKS_DIR)
    # a zombie lease exists when retry starts...
    db = leases._db()
    db.execute("INSERT INTO lease VALUES ('0005','ZOMBIE',999999,'2000-01-01T00:00:00+00:00','n')")
    db.commit()
    db.close()
    real_get = leases.get

    def racing_get(tid: str) -> object:
        row = real_get(tid)
        if tid == "0005" and row is not None and row.token == "ZOMBIE":
            leases.clear_if("0005", "ZOMBIE")  # another lane reaps it...
            leases.claim("0005", "FRESH")  # ...and takes a live lease
        return row

    monkeypatch.setattr(leases, "get", racing_get)
    res = intents.retry("0005")
    assert not res.ok  # the fresh lease is respected, retry declines
    assert real_get("0005").token == "FRESH"  # NOT clobbered by the token-blind path


def test_add_inbox_appends_atomically() -> None:
    config.QUEUE_INBOX.parent.mkdir(parents=True)
    config.QUEUE_INBOX.write_text("- demo: existing line\n")
    res = intents.add_inbox("platform-toolkit", "  the badge   shows stale status  ")
    assert res.ok
    text = config.QUEUE_INBOX.read_text()
    assert text == "- demo: existing line\n- platform-toolkit: the badge shows stale status\n"


def test_add_inbox_needs_both_fields() -> None:
    assert intents.add_inbox("", "something").ok is False
    assert intents.add_inbox("repo", "   ").ok is False


def test_add_inbox_is_serialized_under_load() -> None:
    from concurrent.futures import ThreadPoolExecutor

    config.QUEUE_INBOX.parent.mkdir(parents=True)
    config.QUEUE_INBOX.write_text("")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: intents.add_inbox("r", f"item {n}"), range(60)))
    lines = [ln for ln in config.QUEUE_INBOX.read_text().splitlines() if ln.strip()]
    assert len(lines) == 60  # nothing lost
    assert len({ln for ln in lines}) == 60


def test_comment_planned_preserves_a_concurrent_append() -> None:
    config.QUEUE_INBOX.parent.mkdir(parents=True)
    config.QUEUE_INBOX.write_text("- demo: one\n- demo: two\n")
    intents.add_inbox("demo", "three")  # arrived after planning read the top
    intents.comment_planned({"- demo: one", "- demo: two"})
    text = config.QUEUE_INBOX.read_text()
    assert text == (
        "# planned: - demo: one\n# planned: - demo: two\n- demo: three\n"
    )


def test_run_and_plan_spawn_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeProc:
        pid = 4242

    def _fake_popen(argv, **kw):  # type: ignore[no-untyped-def]
        calls.append({"argv": argv, "kw": kw})
        return _FakeProc()

    monkeypatch.setattr(intents.subprocess, "Popen", _fake_popen)
    r = intents.run(max_n=3)
    assert r.ok and r.data["pid"] == 4242
    assert calls[0]["kw"]["start_new_session"] is True
    assert "--max" in calls[0]["argv"] and "3" in calls[0]["argv"]
    intents.plan()
    assert calls[1]["argv"][-1] == "plan"