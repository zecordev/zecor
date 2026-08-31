# SPDX-License-Identifier: Apache-2.0
"""CLI wiring -- plan/run/status against a temp ZECOR_ROOT."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import cli, config
from zecor.cards import Card
from zecor.orchestrator import TaskOutcome
from zecor.task import Klass


@pytest.fixture(autouse=True)
def _fleet_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "zecor"
    for name, sub in [
        ("ZECOR_ROOT", ""),
        ("TASKS_DIR", "tasks"),
        ("LANES_DIR", "lanes"),
        ("DIGEST_DIR", "digest"),
        ("LOGS_DIR", "logs"),
        ("QUEUE_INBOX", "queue/inbox.md"),
        ("DB_PATH", "db/zecor.db"),
        ("STOP_FILE", "STOP"),
    ]:
        monkeypatch.setattr(config, name, root / sub if sub else root)
    (root / "queue").mkdir(parents=True)
    config.QUEUE_INBOX.write_text("- demo: make the gate pass\n- other: ship a thing\n")
    # `zecor run` preflight probes the real machine (local model, swap, disk); keep the
    # CLI wiring tests hermetic regardless of the host's actual free space. test_preflight
    # and the disk-floor tests cover those probes on their own terms.
    monkeypatch.setattr(cli, "_preflight", lambda: ([], []))
    monkeypatch.setattr(cli, "_disk_free_gb", lambda: 999)
    return root


def test_plan_writes_cards_and_comments_the_inbox(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_plan_item(item, next_id):
        return Card(
            id=next_id,
            repo=item.repo,
            intent=item.intent,
            verify_cmd="./.fleet/verify.sh",
            allow_files=[],
            klass=Klass.BOUNDED,
        )

    monkeypatch.setattr(cli, "plan_item", fake_plan_item)
    assert cli.main(["plan"]) == 0
    cards = sorted(config.TASKS_DIR.glob("*.md"))
    assert [c.name for c in cards] == ["0001.md", "0002.md"]
    assert config.QUEUE_INBOX.read_text().count("# planned:") == 2


def test_plan_leaves_an_inbox_line_uncommented_when_create_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # create() raising CardExists (a concurrent planner / id collision) must NOT mark
    # that inbox line planned, or the work is silently lost
    from zecor.core import cards as core_cards

    monkeypatch.setattr(
        cli, "plan_item",
        lambda item, next_id: Card(next_id, item.repo, item.intent, "./v.sh", []),
    )
    real_create = core_cards.create

    def flaky_create(card: Card):  # type: ignore[no-untyped-def]
        if card.id == "0001":
            raise core_cards.CardExists("0001")
        return real_create(card)

    monkeypatch.setattr(core_cards, "create", flaky_create)
    assert cli.main(["plan"]) == 0
    inbox = config.QUEUE_INBOX.read_text().splitlines()
    assert inbox[0] == "- demo: make the gate pass"  # 0001 refused -> left as-is
    assert inbox[1] == "# planned: - other: ship a thing"  # 0002 created -> commented
    assert not (config.TASKS_DIR / "0001.md").exists()


def test_run_dispatches_ready_cards_and_writes_a_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    Card("0001", "demo", "x", "./.fleet/verify.sh", []).write(_mk(config.TASKS_DIR))
    monkeypatch.setattr(cli, "resolve", lambda repo: Path("/repo"))
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda card, ledger, **kw: TaskOutcome(card.id, "landed", note="ok"),
    )
    assert cli.main(["run"]) == 0
    assert list(config.DIGEST_DIR.glob("*.md"))
    assert Card.parse((config.TASKS_DIR / "0001.md").read_text()).state == "landed"


def test_run_honours_the_stop_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    Card("0001", "demo", "x", "./.fleet/verify.sh", []).write(_mk(config.TASKS_DIR))
    config.STOP_FILE.write_text("halt")
    called: list[str] = []
    monkeypatch.setattr(cli, "resolve", lambda repo: Path("/repo"))
    monkeypatch.setattr(cli, "run_task", lambda *a, **k: called.append("ran"))
    cli.main(["run"])
    assert called == []


def test_run_processes_multiple_cards_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    for i in (1, 2, 3):
        Card(f"{i:04d}", "demo", "x", "./.fleet/verify.sh", []).write(_mk(config.TASKS_DIR))
    seen: list[str] = []
    monkeypatch.setattr(cli, "resolve", lambda repo: Path("/repo"))

    def fake_run(card, ledger, **kw):
        seen.append(card.id)
        assert "git_lock" in kw
        return TaskOutcome(card.id, "landed", note="ok")

    monkeypatch.setattr(cli, "run_task", fake_run)
    assert cli.main(["run"]) == 0
    assert sorted(seen) == ["0001", "0002", "0003"]
    for i in (1, 2, 3):
        assert Card.parse((config.TASKS_DIR / f"{i:04d}.md").read_text()).state == "landed"


def test_deferred_cards_are_picked_up_on_the_next_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # regression: a dry-window `deferred` card fell out of the queue permanently.
    c = Card("0001", "demo", "x", "./.fleet/verify.sh", [])
    c.state = "deferred"
    c.write(_mk(config.TASKS_DIR))
    seen: list[str] = []
    monkeypatch.setattr(cli, "resolve", lambda repo: Path("/repo"))
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda card, ledger, **kw: seen.append(card.id) or TaskOutcome(card.id, "landed"),
    )
    cli.main(["run"])
    assert seen == ["0001"]


def test_deferred_card_within_cooldown_is_held_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # regression: a deferred card was retried on every run -- a launchd loop re-burned
    # the escalation ladder every few minutes on a card only going to defer again.
    from datetime import UTC, datetime, timedelta

    c = Card("0001", "demo", "x", "./.fleet/verify.sh", [])
    c.state = "deferred"
    c.retry_after = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    c.write(_mk(config.TASKS_DIR))
    d = Card("0002", "demo", "y", "./.fleet/verify.sh", [])
    d.state = "deferred"
    d.retry_after = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()  # due
    d.write(config.TASKS_DIR)

    seen: list[str] = []
    monkeypatch.setattr(cli, "resolve", lambda repo: Path("/repo"))
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda card, ledger, **kw: seen.append(card.id) or TaskOutcome(card.id, "landed"),
    )
    cli.main(["run"])
    assert seen == ["0002"]  # 0001 still in its hold window


def test_marking_deferred_sets_a_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    Card("0001", "demo", "x", "./.fleet/verify.sh", []).write(_mk(config.TASKS_DIR))
    monkeypatch.setattr(cli, "resolve", lambda repo: Path("/repo"))
    monkeypatch.setattr(
        cli, "run_task", lambda card, ledger, **kw: TaskOutcome(card.id, "deferred", note="dry")
    )
    cli.main(["run"])
    got = Card.parse((config.TASKS_DIR / "0001.md").read_text())
    assert got.state == "deferred"
    assert got.retry_after  # set, so the next run holds it


def test_parse_until_is_local_time_and_the_next_occurrence() -> None:
    from datetime import datetime

    got = cli._parse_until("07:00")
    assert got is not None
    assert got.tzinfo is not None  # aware, local
    assert got.hour == 7 and got.minute == 0
    assert got > datetime.now().astimezone()  # next occurrence, never in the past


def test_status_runs_clean_on_a_fresh_tree() -> None:
    assert cli.main(["status"]) == 0


def _mk(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_run_aborts_on_a_preflight_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    Card("0001", "demo", "x", "./.fleet/verify.sh", []).write(_mk(config.TASKS_DIR))
    monkeypatch.setattr(cli, "_preflight", lambda: (["local model not reachable"], []))
    ran: list[str] = []
    monkeypatch.setattr(cli, "run_task", lambda *a, **k: ran.append("x"))
    assert cli.main(["run"]) == 1
    assert ran == []
    assert cli.main(["run", "--force"]) == 0  # --force overrides


def test_ready_cards_holds_a_card_until_its_dep_lands(monkeypatch: pytest.MonkeyPatch) -> None:
    a = Card("0001", "demo", "x", "./.fleet/verify.sh", ["s.py"])
    b = Card("0002", "demo", "y", "./.fleet/verify.sh", ["s.py"])
    b.depends_on = ["0001"]
    a.write(_mk(config.TASKS_DIR))
    b.write(config.TASKS_DIR)
    assert [c.id for c in cli._ready_cards()] == ["0001"]  # 0002 held
    a.state = "landed"
    a.write(config.TASKS_DIR)
    assert sorted(c.id for c in cli._ready_cards()) == ["0002"]  # 0001 landed -> 0002 frees


def test_retry_requeues_with_a_forced_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    c = Card("0007", "demo", "x", "./.fleet/verify.sh", [])
    c.state = "needs-human-design"
    c.write(_mk(config.TASKS_DIR))
    monkeypatch.setattr(config, "DB_PATH", config.ZECOR_ROOT / "db" / "zecor.db")
    args = ["retry", "0007", "--tier", "t4", "--model", "opus", "--note", "use a set"]
    assert cli.main(args) == 0
    got = Card.parse((config.TASKS_DIR / "0007.md").read_text())
    assert got.state == "ready"
    assert got.retry_tier == "t4" and got.retry_model == "opus"
    assert got.retry_context == "use a set" and got.retry_count == 1


def test_retry_refuses_a_landed_card() -> None:
    c = Card("0008", "demo", "x", "./.fleet/verify.sh", [])
    c.state = "landed"
    c.write(_mk(config.TASKS_DIR))
    assert cli.main(["retry", "0008"]) == 1


def test_retry_declines_while_a_lane_holds_a_live_lease() -> None:
    from zecor.core import leases

    c = Card("0009", "demo", "x", "./.fleet/verify.sh", [])
    c.state = "blocked"
    c.write(_mk(config.TASKS_DIR))
    leases.claim("0009", "a-live-run")
    assert cli.main(["retry", "0009"]) == 1  # a run currently holds it
    assert Card.parse((config.TASKS_DIR / "0009.md").read_text()).state == "blocked"