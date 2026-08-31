# SPDX-License-Identifier: Apache-2.0
"""Test doubles. Synthetic ledger + a throwaway git repo for lane/loop tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from zecor.tiers import Tier


class FakeLedger:
    """Synthetic ledger for property tests and the orchestrator integration test."""

    def __init__(self, *, dry: set[Tier] | None = None, month_spend: float = 0.0) -> None:
        self._dry = set(dry or set())
        self._month_spend = month_spend
        self.deadends: set[str] = set()
        self.attempts: list[dict[str, object]] = []
        self.ratelimits: list[dict[str, object]] = []
        self.marked_dry: list[Tier] = []

    def has(self, tier: Tier) -> bool:
        if tier in (Tier.T0, Tier.T1):
            return True
        return tier not in self._dry

    def mark_dry(self, tier: Tier, **kw: object) -> None:
        self.marked_dry.append(tier)
        if float(kw.get("hours", 1)) > 0:  # type: ignore[arg-type]
            self._dry.add(tier)

    def month_spend_usd(self) -> float:
        return self._month_spend

    def is_deadend(self, fingerprint: str) -> bool:
        return fingerprint in self.deadends

    def note_deadend(self, fingerprint: str, approach: str, note: str) -> None:
        self.deadends.add(fingerprint)

    def record_attempt(self, **kw: object) -> None:
        self.attempts.append(kw)

    def observe_ratelimit(self, tier: Tier, **kw: object) -> None:
        self.ratelimits.append({"tier": tier, **kw})


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(root: Path, *, verify_body: str = "exit 0", source: str = "// v0\n") -> Path:
    """A committed git repo with a .fleet/verify.sh and one source file."""
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    (root / ".fleet").mkdir(exist_ok=True)
    gate = root / ".fleet" / "verify.sh"
    gate.write_text(f"#!/bin/sh\n{verify_body}\n")
    gate.chmod(0o755)
    (root / "src.txt").write_text(source)
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "init"], root)
    return root


@dataclass
class ScriptedWorker:
    """A dispatcher stand-in: each call runs the next action from `script`.

    An action is a callable(lane_dir: Path) -> WorkerResult-ish tuple
    (outcome, verify_exit). It may mutate the lane's working tree.
    """

    actions: list  # list[Callable[[Path], tuple[str, int]]]
    calls: list = field(default_factory=list)

    def __call__(self, route, job):
        from zecor.workers.base import RateLimit, VerifyRun, WorkerResult

        idx = min(len(self.calls), len(self.actions) - 1)
        outcome, exit_code = self.actions[idx](job.lane_dir)
        self.calls.append((route.tier.value, route.model))
        if outcome in ("refused", "timeout", "ratelimited"):
            rl = RateLimit.rolling_5h("x") if outcome == "ratelimited" else None
            return WorkerResult(outcome, stdout=f"scripted {outcome}", verify=None, rate_limit=rl)
        return WorkerResult(
            outcome,
            stdout=f"scripted {outcome}",
            verify=VerifyRun(exit_code, f"exit {exit_code}", ""),
        )