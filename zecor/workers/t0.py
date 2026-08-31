# SPDX-License-Identifier: Apache-2.0
"""The T0 worker: apply confirmed deterministic rules with ast-grep, then verify. No
model, no tokens -- a card only reaches here when the planner already found a matching
active rule. If the rules rewrite nothing, that's a `refused` outcome and the
orchestrator escalates to T1 exactly as for a model decline."""

from __future__ import annotations

from zecor import t0
from zecor.workers.base import Job, WorkerResult, run_verify


class T0Worker:
    tier_name = "t0"

    def run(self, job: Job) -> WorkerResult:
        if not t0.available():
            return WorkerResult(outcome="refused", stderr="ast-grep not installed")
        rules = t0.select(t0.load_active(), job.repo, job.intent)
        if not rules:
            return WorkerResult(outcome="refused", stderr="no active rule matches this card")
        applied = t0.apply(rules, job.lane_dir, timeout_s=min(job.timeout_s, 120.0))
        if not applied.changed:
            return WorkerResult(outcome="refused", stdout=applied.note)
        vr = run_verify(job)
        return WorkerResult(
            outcome="pass" if vr.ok else "fail",
            stdout=applied.note,
            verify=vr,
            flags=["t0", *applied.rules_run],
        )