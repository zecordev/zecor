# SPDX-License-Identifier: Apache-2.0
"""T2b -- Antigravity CLI `agy` (Google AI Pro). Reserved for its actual monopoly:
native multimodal input (video/audio/PDF) and huge single-shot context -- 1M in but only
~65K out, a reader not a writer. Never route routine coding here: ~23-25K tokens/request
of fixed overhead against a hard weekly cap. Needs one interactive browser sign-in per
machine -- there is no API-key env var."""

from __future__ import annotations

import json
import os

from zecor.workers.base import Job, RateLimit, WorkerResult, resolve_tool, run_capped


class AntigravityWorker:
    tier_name = "T2b"

    def run(self, job: Job) -> WorkerResult:
        # --dangerously-skip-permissions: unattended, no one to approve an edit. --model:
        # route()'s pick, else ZECOR_ANTIGRAVITY_MODEL, else agy's own default (the
        # ledger then records model=None -- an honest "default").
        cmd = [resolve_tool("agy"), "-p", job.prompt, "--output-format", "json",
               "--dangerously-skip-permissions", "--mode", "accept-edits"]
        model = job.model or os.environ.get("ZECOR_ANTIGRAVITY_MODEL")
        if model:
            cmd += ["--model", model]
        rc, out, err, killed = run_capped(cmd, cwd=job.lane_dir, timeout_s=job.timeout_s)
        if killed:
            return WorkerResult("timeout", out, err)

        blob = (out + err).lower()
        if "auth" in blob and ("sign in" in blob or "unauthenticated" in blob or "login" in blob):
            return WorkerResult(
                "refused",
                out,
                err,
                flags=["agy not signed in -- one interactive browser OAuth needed"],
            )
        if "quota" in blob or "rate limit" in blob or "429" in blob:
            return WorkerResult(
                "ratelimited", out, err, rate_limit=RateLimit.weekly("google-aipro")
            )

        in_tok, out_tok = _usage(out)
        # T2b is a reader: pass means it returned text, not that a lane changed.
        return WorkerResult(
            "pass" if rc == 0 and out.strip() else "fail", out, err, in_tok, out_tok
        )


def _usage(raw: str) -> tuple[int, int]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0, 0
    u = data.get("usage", {}) if isinstance(data, dict) else {}
    return int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))