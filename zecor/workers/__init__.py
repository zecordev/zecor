# SPDX-License-Identifier: Apache-2.0
"""One interface, six workers: `zecor` is the only thing that talks to the vendor
CLIs. Route -> Worker here."""

from __future__ import annotations

from zecor.route import Route
from zecor.tiers import Tier
from zecor.workers.antigravity import AntigravityWorker
from zecor.workers.base import Job, Worker, WorkerResult
from zecor.workers.claude_code import ClaudeWorker
from zecor.workers.codex import CodexWorker
from zecor.workers.gemini import GeminiWorker
from zecor.workers.local import LocalWorker
from zecor.workers.luna import LunaWorker
from zecor.workers.t0 import T0Worker

_REGISTRY: dict[Tier, type[Worker]] = {
    Tier.T0: T0Worker,
    Tier.T1: LocalWorker,
    Tier.T2A: GeminiWorker,
    Tier.T2B: AntigravityWorker,
    Tier.T3: CodexWorker,
    Tier.T4: ClaudeWorker,
    Tier.T5: LunaWorker,
}


class NoWorkerForTier(LookupError):
    """Only the control outcomes (NEEDS_HUMAN, DEFER) have no worker."""


def worker_for(tier: Tier) -> Worker:
    try:
        return _REGISTRY[tier]()
    except KeyError as e:
        raise NoWorkerForTier(f"no worker for {tier}") from e


def dispatch(route: Route, job: Job) -> WorkerResult:
    return worker_for(route.tier).run(job)


__all__ = [
    "Job",
    "NoWorkerForTier",
    "Worker",
    "WorkerResult",
    "dispatch",
    "worker_for",
]