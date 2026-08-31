# SPDX-License-Identifier: Apache-2.0
"""provenance: git-note + jsonl record for AI-generated code (build-queue item A)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from zecor import config, provenance


@dataclass
class _Attempt:
    tier: str
    model: str | None


@dataclass
class _Out:
    attempts: list[_Attempt] = field(default_factory=list)
    state: str = "landed"
    review: object = None


@dataclass
class _Card:
    id: str = "0042"
    repo: str = "demo"


@dataclass
class _Review:
    verdict: str


def test_gather_dedupes_models_and_tiers() -> None:
    out = _Out(
        attempts=[
            _Attempt("t1", None),
            _Attempt("t1", None),
            _Attempt("t3", "codex/gpt-x"),
            _Attempt("t4", "claude-opus-5"),
        ],
        review=_Review("ship"),
    )
    p = provenance.gather(_Card(), out)
    assert p.models == ("local:t1", "codex/gpt-x", "claude-opus-5")
    assert p.tiers == ("t1", "t3", "t4")
    assert p.human_review == "self-review-clean"
    assert p.attempts == 4


def test_review_state_mapping() -> None:
    assert provenance.gather(_Card(), _Out(review=_Review("block"))).human_review == (
        "self-review-blocked"
    )
    assert provenance.gather(
        _Card(), _Out(state="needs-human-design", review=_Review("needs-work"))
    ).human_review == "human-required"
    assert provenance.gather(_Card(), _Out()).human_review == "none"


def test_decorate_only_for_opted_in_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    p = provenance.gather(_Card(repo="secret"), _Out(attempts=[_Attempt("t4", "opus-5")]))
    assert provenance.decorate("do a thing", p, repo="secret") == "do a thing"  # not opted in
    monkeypatch.setattr(provenance, "_TRAILER_REPOS", frozenset({"secret"}))
    out = provenance.decorate("do a thing", p, repo="secret")
    assert "AI-Assisted: yes" in out and "AI-Model: opus-5" in out and "AI-Task-Ref: 0042" in out
    assert provenance.decorate(out, p, repo="secret") == out  # idempotent
    # an intent that merely mentions the string must NOT suppress the block
    got = provenance.decorate("mention AI-Assisted: in the body text", p, repo="secret")
    assert got.count("AI-Assisted:") == 2  # the mention + the real trailer


def test_record_writes_a_note_and_a_jsonl_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "ZECOR_ROOT", tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()

    out = _Out(attempts=[_Attempt("t1", None), _Attempt("t3", "codex")], review=_Review("ship"))
    status = provenance.record(_Card(), out, repo, repo="demo", sha=sha)
    assert "note+jsonl" in status  # both landed

    note = subprocess.run(
        ["git", "notes", f"--ref={provenance.NOTES_REF}", "show", sha],
        cwd=repo, capture_output=True, text=True,
    ).stdout
    rec = json.loads(note)
    assert rec["task"] == "0042" and rec["models"] == ["local:t1", "codex"]
    assert rec["commit"] == sha and rec["human_review"] == "self-review-clean"

    jsonl = list((tmp_path / "provenance").glob("*.jsonl"))
    assert len(jsonl) == 1
    assert json.loads(jsonl[0].read_text())["commit"] == sha


def test_record_survives_a_non_git_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ZECOR_ROOT", tmp_path)
    status = provenance.record(_Card(), _Out(), tmp_path / "nope", repo="demo", sha="deadbeef")
    assert "jsonl" in status  # the note fails, the export still lands