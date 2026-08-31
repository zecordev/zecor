# SPDX-License-Identifier: Apache-2.0
"""T1 -- local model via aider against LM Studio. Runs through aider's search/replace
diff blocks, not a tool-calling agent loop -- local models are mediocre at long
multi-turn tool sequences but good at emitting diff blocks."""

from __future__ import annotations

from pathlib import Path

from zecor.workers.base import Job, WorkerResult, resolve_tool, run_capped, run_verify

# repo root -- zecor/workers/local.py -> parents[2]. Move-proof.
AIDER_CONFIG = Path(__file__).resolve().parents[2] / "aider.local.yml"


class LocalWorker:
    tier_name = "T1"

    def __init__(self, config: Path = AIDER_CONFIG) -> None:
        self.config = config

    def run(self, job: Job) -> WorkerResult:
        cmd = [
            resolve_tool("aider"),
            "--config",
            str(self.config),
            "--yes-always",
            "--no-auto-commits",
            "--no-gitignore",
            "--no-show-model-warnings",
            "--no-check-update",
        ]
        for p in job.readonly:  # optional: paths given as context, not editable
            cmd += ["--read", p]
        cmd += ["--message", job.prompt]
        env = {"AIDER_ANALYTICS": "false", "OR_SITE_URL": "", "BROWSER": "true"}
        rc, out, err, killed = run_capped(
            cmd, cwd=job.lane_dir, timeout_s=job.timeout_s, extra_env=env
        )
        if killed:
            return WorkerResult("timeout", out, err)
        # aider's own exit code is unreliable; the gate is the judge.
        verify = run_verify(job)
        outcome = "pass" if verify.ok else "fail"
        # T1 tokens are wall-clock + fan noise, not budget -- record 0.
        return WorkerResult(outcome, out, err, in_tokens=0, out_tokens=0, verify=verify)