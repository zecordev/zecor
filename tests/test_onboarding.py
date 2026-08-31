# SPDX-License-Identifier: Apache-2.0
"""zecor.onboarding: `zecor init` -- config seed, repo/gate check, sample card."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import config, doctor, onboarding, repos


@pytest.fixture(autouse=True)
def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "zecor"
    monkeypatch.setattr(config, "ZECOR_ROOT", root)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(config, "TASKS_DIR", root / "tasks")
    monkeypatch.setattr(config, "LANES_DIR", root / "lanes")
    monkeypatch.setattr(config, "DIGEST_DIR", root / "digest")
    monkeypatch.setattr(config, "LOGS_DIR", root / "logs")
    monkeypatch.setattr(config, "QUEUE_INBOX", root / "queue" / "inbox.md")
    monkeypatch.setattr(config, "DB_PATH", root / "db" / "zecor.db")
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "zecor.config.example.json").write_text('{"mode": "hardened"}')
    monkeypatch.setattr(doctor, "report", lambda: ("0 gap(s)  -- ready", 0))
    return root


def test_seeds_config_from_the_example_when_missing() -> None:
    text, code = onboarding.run(repo=None, do_install=False)
    assert code == 0
    dst = config.ZECOR_ROOT / "zecor.config.json"
    assert dst.exists()
    assert dst.read_text() == '{"mode": "hardened"}'
    assert "made  zecor.config.json" in text


def test_leaves_an_existing_config_alone() -> None:
    dst_dir = config.ZECOR_ROOT
    dst_dir.mkdir(parents=True)
    (dst_dir / "zecor.config.json").write_text('{"mode": "custom"}')
    text, code = onboarding.run(repo=None, do_install=False)
    assert code == 0
    assert (dst_dir / "zecor.config.json").read_text() == '{"mode": "custom"}'
    assert "ok    zecor.config.json" in text


def test_no_repo_given_is_not_an_error() -> None:
    text, code = onboarding.run(repo=None, do_install=False)
    assert code == 0
    assert "no --repo given" in text
    assert not config.QUEUE_INBOX.exists()


def test_unknown_repo_reports_where_it_looked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        repos, "resolve",
        lambda name: (_ for _ in ()).throw(repos.RepoNotFound(f"no checkout named {name!r}")),
    )
    text, code = onboarding.run(repo="ghost", do_install=False)
    assert code == 1
    assert "no checkout named 'ghost'" in text
    assert not config.QUEUE_INBOX.exists()


def test_repo_with_no_gate_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "widget"
    checkout.mkdir()
    monkeypatch.setattr(repos, "resolve", lambda name: checkout)
    monkeypatch.setattr(repos, "gate_path", lambda path: None)
    text, code = onboarding.run(repo="widget", do_install=False)
    assert code == 1
    assert "no .zecor/verify.sh" in text
    assert "zecor scaffold" in text
    assert not config.QUEUE_INBOX.exists()


def test_repo_with_a_gate_queues_one_sample_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "widget"
    (checkout / ".zecor").mkdir(parents=True)
    gate = checkout / ".zecor" / "verify.sh"
    gate.write_text("#!/bin/sh\n")
    monkeypatch.setattr(repos, "resolve", lambda name: checkout)
    monkeypatch.setattr(repos, "gate_path", lambda path: gate)
    text, code = onboarding.run(repo="widget", do_install=False)
    assert code == 0
    assert "queued a sample task" in text
    lines = config.QUEUE_INBOX.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("- widget: ")

    # re-running init for the same repo must not duplicate the line
    text2, code2 = onboarding.run(repo="widget", do_install=False)
    assert code2 == 0
    assert "already queued" in text2
    assert len(config.QUEUE_INBOX.read_text().splitlines()) == 1
