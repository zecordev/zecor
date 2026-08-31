# SPDX-License-Identifier: Apache-2.0
"""Web deploy pipeline -- SHIP-IT.md step 4. INERT until turned on.

`deploy(repo, sha)` builds a container and rolls it out to Cloud Run; with
`ZECOR_AUTODEPLOY` unset/0 it instead records a `pending_deploy` row for one-click
approval in the console. `rollback()` shifts 100% traffic back to the previous
revision. Nothing calls `deploy()` yet -- it's written and tested (gcloud mocked) so
wiring it later is a one-line change, and safe to ship dormant since every entry point
refuses without `ZECOR_GCP_PROJECT`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

from zecor import config

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS pending_deploy ("
    " id INTEGER PRIMARY KEY, repo TEXT NOT NULL, sha TEXT NOT NULL,"
    " requested_at TEXT NOT NULL, resolved_at TEXT, resolution TEXT)"
)


def project() -> str:
    return os.environ.get("ZECOR_GCP_PROJECT", "").strip()


def region() -> str:
    return os.environ.get("ZECOR_GCP_REGION", "us-central1").strip()


def autodeploy_on() -> bool:
    return os.environ.get("ZECOR_AUTODEPLOY", "").strip() not in ("", "0", "false", "no")


def configured() -> bool:
    return bool(project()) and shutil.which("gcloud") is not None


@dataclass(frozen=True)
class DeployResult:
    ok: bool
    revision: str = ""
    url: str = ""
    note: str = ""


def _gcloud(args: list[str], *, timeout_s: float = 900.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            ["gcloud", *args, f"--project={project()}", "--quiet"],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, "", str(e)


def deploy(repo: str, sha: str, *, service: str | None = None) -> DeployResult:
    """Build `repo` at `sha` and roll it out to Cloud Run. Refuses if GCP is not wired."""
    if not configured():
        return DeployResult(False, note="GCP not configured (ZECOR_GCP_PROJECT / gcloud)")
    svc = service or repo
    image = f"{region()}-docker.pkg.dev/{project()}/zecor/{repo}:{sha[:12]}"
    rc, _o, err = _gcloud(["builds", "submit", "--tag", image, "."])
    if rc != 0:
        return DeployResult(False, note=f"build failed: {err[:200]}")
    rc, out, err = _gcloud([
        "run", "deploy", svc, "--image", image, "--region", region(),
        "--platform", "managed", "--format=value(status.latestCreatedRevisionName,status.url)",
    ])
    if rc != 0:
        return DeployResult(False, note=f"run deploy failed: {err[:200]}")
    parts = out.split("\t")
    rev = parts[0].strip() if parts else ""
    url = parts[1].strip() if len(parts) > 1 else ""
    return DeployResult(True, revision=rev, url=url, note="deployed")


def rollback(service: str, *, to_revision: str = "") -> DeployResult:
    if not configured():
        return DeployResult(False, note="GCP not configured")
    target = to_revision or _previous_revision(service)
    if not target:
        return DeployResult(False, note="no previous revision to roll back to")
    rc, _o, err = _gcloud([
        "run", "services", "update-traffic", service, "--region", region(),
        "--to-revisions", f"{target}=100",
    ])
    return (
        DeployResult(True, revision=target, note=f"rolled back to {target}")
        if rc == 0
        else DeployResult(False, note=f"rollback failed: {err[:200]}")
    )


def _previous_revision(service: str) -> str:
    rc, out, _e = _gcloud([
        "run", "revisions", "list", "--service", service, "--region", region(),
        "--format=value(metadata.name)", "--sort-by=~metadata.creationTimestamp", "--limit=2",
    ])
    lines = out.splitlines() if rc == 0 else []
    return lines[1].strip() if len(lines) > 1 else ""


# -- the pending-deploy queue (ZECOR_AUTODEPLOY off) ------------------------

def _db() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH, timeout=15.0)
    db.row_factory = sqlite3.Row
    db.execute(_SCHEMA)
    db.commit()
    return db


def queue_pending(repo: str, sha: str) -> int:
    db = _db()
    try:
        cur = db.execute(
            "INSERT INTO pending_deploy (repo, sha, requested_at) VALUES (?,?,?)",
            (repo, sha, datetime.now(UTC).isoformat()),
        )
        db.commit()
        return int(cur.lastrowid or 0)
    finally:
        db.close()


@dataclass(frozen=True)
class Pending:
    id: int
    repo: str
    sha: str
    requested_at: str


def list_pending() -> list[Pending]:
    db = _db()
    try:
        rows = db.execute(
            "SELECT id, repo, sha, requested_at FROM pending_deploy "
            "WHERE resolved_at IS NULL ORDER BY id"
        ).fetchall()
    finally:
        db.close()
    return [Pending(r["id"], r["repo"], r["sha"], r["requested_at"]) for r in rows]


def _peek(pid: int) -> Pending | None:
    db = _db()
    try:
        row = db.execute(
            "SELECT id, repo, sha, requested_at FROM pending_deploy "
            "WHERE id = ? AND resolved_at IS NULL",
            (pid,),
        ).fetchone()
    finally:
        db.close()
    return None if row is None else Pending(row["id"], row["repo"], row["sha"], row["requested_at"])


def _resolve(pid: int, resolution: str) -> bool:
    """Atomically mark a still-unresolved row resolved. Returns whether it was ours to
    resolve -- so two concurrent approve/reject calls cannot both act on one row."""
    db = _db()
    try:
        cur = db.execute(
            "UPDATE pending_deploy SET resolved_at = ?, resolution = ? "
            "WHERE id = ? AND resolved_at IS NULL",
            (datetime.now(UTC).isoformat(), resolution, pid),
        )
        db.commit()
        return cur.rowcount > 0
    finally:
        db.close()


def approve_pending(pid: int) -> DeployResult:
    """Deploy first; only mark the row resolved if the deploy actually succeeded, so an
    unconfigured or failed deploy leaves it in the queue to retry."""
    p = _peek(pid)
    if p is None:
        return DeployResult(False, note=f"no pending deploy {pid}")
    r = deploy(p.repo, p.sha)
    if r.ok and _resolve(pid, "deployed"):
        return r
    return DeployResult(False, revision=r.revision, note=r.note + " -- left pending")


def reject_pending(pid: int) -> bool:
    return _resolve(pid, "rejected")


def on_merge(repo: str, sha: str) -> str:
    """The hook the auto-merge gate will call after a merge to main. Deploys now if
    ZECOR_AUTODEPLOY, queues a pending deploy if GCP is wired but the flag is off, and
    does nothing at all if GCP is not configured."""
    if not configured():
        return "deploy skipped -- GCP not configured"
    if autodeploy_on():
        r = deploy(repo, sha)
        return f"auto-deploy: {r.note}" + (f" ({r.revision})" if r.ok else "")
    pid = queue_pending(repo, sha)
    return f"deploy held -- pending #{pid} (approve in the console)"