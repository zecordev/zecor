# SPDX-License-Identifier: Apache-2.0
"""Provenance for AI-generated code -- build-queue item A.

Every landed branch gets a record of which models and tiers produced it and whether a
human reviewed it, written as a git note on `refs/notes/provenance` and appended to
`provenance/<YYYY-MM>.jsonl`. Notes, not commit trailers, because `git` doesn't
push/fetch notes by default -- a public repo can push the ref to a private mirror only
and keep model use off public history; a repo can opt into trailers instead with
`ZECOR_PROVENANCE_TRAILERS=<repo>,<repo>`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from zecor import config

NOTES_REF = "refs/notes/provenance"
# Repos whose commit messages get the AI-* trailers (comma list). Everything else is
# notes-only. `platform-toolkit` deliberately stays notes-only (public history).
_TRAILER_REPOS = frozenset(
    r.strip() for r in os.environ.get("ZECOR_PROVENANCE_TRAILERS", "").split(",") if r.strip()
)


@dataclass(frozen=True)
class Provenance:
    task_ref: str
    models: tuple[str, ...]  # every distinct model that touched the branch, in order
    tiers: tuple[str, ...]
    human_review: str  # none | self-review-clean | self-review-blocked | human-required
    attempts: int
    landed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def _review_state(out: object) -> str:
    review = getattr(out, "review", None)
    verdict = getattr(review, "verdict", None)
    if verdict == "block":
        return "self-review-blocked"
    if getattr(out, "state", "") == "needs-human-design":
        return "human-required"
    if verdict in ("ship", "local-only", "no-review", "needs-work"):
        return "self-review-clean"
    return "none"


def gather(card: object, out: object) -> Provenance:
    """Build the record from a TaskOutcome. `out.attempts` (AttemptLog) is the authority
    on tier+model; the ledger is not consulted so this works before a commit is made."""
    attempts = list(getattr(out, "attempts", []) or [])
    models: list[str] = []
    tiers: list[str] = []
    for a in attempts:
        m = getattr(a, "model", None) or f"local:{getattr(a, 'tier', '?')}"
        if m not in models:
            models.append(m)
        t = getattr(a, "tier", None)
        if t and t not in tiers:
            tiers.append(t)
    return Provenance(
        task_ref=str(getattr(card, "id", "?")),
        models=tuple(models) or ("unknown",),
        tiers=tuple(tiers) or ("t1",),
        human_review=_review_state(out),
        attempts=len(attempts),
    )


def trailers(p: Provenance) -> list[str]:
    return [
        "AI-Assisted: yes",
        f"AI-Model: {', '.join(p.models)}",
        f"AI-Tier: {', '.join(p.tiers)}",
        f"AI-Task-Ref: {p.task_ref}",
        f"AI-Human-Review: {p.human_review}",
    ]


def wants_trailers(repo: str) -> bool:
    return repo in _TRAILER_REPOS


_TRAILER_LINE = re.compile(r"^AI-Assisted:\s", re.M)


def decorate(message: str, p: Provenance, *, repo: str) -> str:
    """Append the AI-* trailers to a commit message iff `repo` opted in. Idempotent --
    keyed on an actual trailer line, not a substring, so an intent that mentions
    "AI-Assisted:" does not suppress the block."""
    if not wants_trailers(repo) or _TRAILER_LINE.search(message):
        return message
    body = message.rstrip()
    return f"{body}\n\n" + "\n".join(trailers(p)) + "\n"


def as_json(p: Provenance, *, repo: str, sha: str) -> str:
    return json.dumps(
        {
            "repo": repo,
            "commit": sha,
            "task": p.task_ref,
            "models": list(p.models),
            "tiers": list(p.tiers),
            "human_review": p.human_review,
            "attempts": p.attempts,
            "landed_at": p.landed_at,
        },
        separators=(",", ":"),
    )


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=30
    )


def add_note(lane_path: Path, sha: str, payload: str) -> bool:
    """Attach `payload` to `sha` on refs/notes/provenance. Replaces any existing note so
    a re-land is not a duplicate. Returns whether it stuck."""
    r = _git(["notes", f"--ref={NOTES_REF}", "add", "-f", "-m", payload, sha], lane_path)
    return r.returncode == 0


def export_line(repo: str, sha: str, payload: str) -> Path:
    """Append one JSONL record to provenance/<YYYY-MM>.jsonl in the state tree."""
    month = datetime.now(UTC).strftime("%Y-%m")
    out_dir = config.ZECOR_ROOT / "provenance"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{month}.jsonl"
    with path.open("a") as fh:
        fh.write(payload + "\n")
    return path


def record(card: object, out: object, lane_path: Path, *, repo: str, sha: str) -> str:
    """The land-time hook: write the git note and the JSONL line. Independently
    best-effort -- a provenance failure never un-lands a verified change, and a failed
    note does not cost the export. Returns a one-line status."""
    p = gather(card, out)
    payload = as_json(p, repo=repo, sha=sha)
    try:
        noted = add_note(lane_path, sha, payload)
    except (OSError, subprocess.SubprocessError):
        noted = False
    try:
        export_line(repo, sha, payload)
        exported = True
    except OSError:
        exported = False
    parts = [x for x in ("note" if noted else "", "jsonl" if exported else "") if x]
    return f"provenance: {'+'.join(parts) or 'skipped'} [{', '.join(p.models)}]"