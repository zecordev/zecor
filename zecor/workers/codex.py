# SPDX-License-Identifier: Apache-2.0
"""T3 -- Codex CLI headless (`codex exec "$PROMPT" --json`). Shares its 5-hour bucket
with the IDE and web app, so daytime use eats Zecor's night -- the ledger treats T3
`used` as a lower bound. Strongest lane for Dockerfile/CI/build iteration."""

from __future__ import annotations

import json
import os

from zecor.workers.base import Job, RateLimit, WorkerResult, resolve_tool, run_capped, run_verify


class CodexWorker:
    tier_name = "T3"

    def run(self, job: Job) -> WorkerResult:
        # -s workspace-write: `codex exec` is read-only by default; without it every
        # patch is "rejected by read-only sandbox".
        cmd = [resolve_tool("codex"), "exec", "--json", "--skip-git-repo-check",
               "-s", "workspace-write"]
        # route()'s T3 model name (gpt-5.6-terra) is a logical label, not a real Codex
        # model id -- passing it makes `codex exec` hang. Only forward a real one via
        # ZECOR_CODEX_MODEL; otherwise use the account default.
        real_model = os.environ.get("ZECOR_CODEX_MODEL")
        if real_model:
            cmd += ["-m", real_model]
        cmd.append(job.prompt)

        rc, out, err, killed = run_capped(cmd, cwd=job.lane_dir, timeout_s=job.timeout_s)
        if killed:
            return WorkerResult("timeout", out, err)

        in_tok, out_tok = _usage_from_jsonl(out)
        if "rate limit" in (out + err).lower() or "429" in (out + err):
            return WorkerResult(
                "ratelimited",
                out,
                err,
                in_tok,
                out_tok,
                rate_limit=RateLimit.rolling_5h("openai-plus"),
            )

        verify = run_verify(job)
        return WorkerResult(
            "pass" if verify.ok else "fail", out, err, in_tok, out_tok, verify=verify
        )


def _usage_from_jsonl(raw: str) -> tuple[int, int]:
    """codex --json streams JSONL events; token usage rides the last event that carries it."""
    it = ot = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = evt.get("usage") if isinstance(evt, dict) else None
        if isinstance(usage, dict):
            it = int(usage.get("input_tokens", it))
            ot = int(usage.get("output_tokens", ot))
    return it, ot