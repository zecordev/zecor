# SPDX-License-Identifier: Apache-2.0
"""zecor.deploy -- inert web deploy pipeline (SHIP-IT.md step 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import config, deploy


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db" / "zecor.db")
    monkeypatch.delenv("ZECOR_AUTODEPLOY", raising=False)
    monkeypatch.delenv("ZECOR_GCP_PROJECT", raising=False)


def test_refuses_when_gcp_is_not_configured() -> None:
    assert deploy.configured() is False
    assert deploy.deploy("resolve-web", "abc123").ok is False
    assert deploy.rollback("resolve-web").ok is False


def test_on_merge_queues_a_pending_deploy_when_autodeploy_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZECOR_GCP_PROJECT", "hv")  # configured, but autodeploy off
    monkeypatch.setattr(deploy.shutil, "which", lambda _n: "/usr/bin/gcloud")
    msg = deploy.on_merge("resolve-web", "deadbeef" * 5)
    assert "pending #1" in msg
    (p,) = deploy.list_pending()
    assert p.repo == "resolve-web" and p.sha.startswith("deadbeef")


def test_on_merge_does_nothing_when_gcp_is_not_configured() -> None:
    assert deploy.on_merge("resolve-web", "abc123") == "deploy skipped -- GCP not configured"
    assert deploy.list_pending() == []


def test_approve_and_reject_resolve_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_GCP_PROJECT", "hv")
    monkeypatch.setattr(deploy.shutil, "which", lambda _n: "/usr/bin/gcloud")
    deploy.on_merge("a", "sha-a")
    deploy.on_merge("b", "sha-b")
    assert {p.repo for p in deploy.list_pending()} == {"a", "b"}

    # a failing deploy leaves the row in the queue to retry
    monkeypatch.setattr(
        deploy, "deploy", lambda *a, **k: deploy.DeployResult(False, note="build failed")
    )
    r = deploy.approve_pending(1)
    assert not r.ok and "left pending" in r.note
    assert {p.repo for p in deploy.list_pending()} == {"a", "b"}

    assert deploy.reject_pending(2) is True

    # a succeeding deploy resolves the row
    monkeypatch.setattr(
        deploy, "deploy",
        lambda *a, **k: deploy.DeployResult(True, revision="r1", note="deployed"),
    )
    assert deploy.approve_pending(1).ok is True
    assert deploy.list_pending() == []
    assert deploy.approve_pending(1).note == "no pending deploy 1"  # already resolved


def test_deploy_calls_gcloud_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_GCP_PROJECT", "hilachem-ventures")
    monkeypatch.setattr(deploy.shutil, "which", lambda _n: "/usr/bin/gcloud")
    calls: list[list[str]] = []

    def fake_gcloud(args: list[str], *, timeout_s: float = 900.0) -> tuple[int, str, str]:
        calls.append(args)
        if args[:2] == ["builds", "submit"]:
            return 0, "", ""
        if args[:2] == ["run", "deploy"]:
            # gcloud --format=value(a,b) joins the two fields with a single tab
            return 0, "resolve-web-00042\thttps://resolve-web-xyz.run.app", ""
        return 0, "", ""

    monkeypatch.setattr(deploy, "_gcloud", fake_gcloud)
    r = deploy.deploy("resolve-web", "cafebabecafebabe")
    assert r.ok and r.revision == "resolve-web-00042"
    assert r.url == "https://resolve-web-xyz.run.app"
    assert calls[0][:2] == ["builds", "submit"] and calls[1][:2] == ["run", "deploy"]


def test_on_merge_auto_deploys_when_the_flag_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_AUTODEPLOY", "1")
    monkeypatch.setenv("ZECOR_GCP_PROJECT", "hv")
    monkeypatch.setattr(deploy.shutil, "which", lambda _n: "/usr/bin/gcloud")
    monkeypatch.setattr(
        deploy, "deploy",
        lambda repo, sha, **k: deploy.DeployResult(True, revision="r1", note="deployed"),
    )
    assert deploy.on_merge("resolve-web", "abc") == "auto-deploy: deployed (r1)"
    assert deploy.list_pending() == []  # nothing queued