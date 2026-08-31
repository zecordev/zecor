# SPDX-License-Identifier: Apache-2.0
"""T4 -- Claude Code headless (`claude -p "$PROMPT" --output-format json`). MCP is
disabled on every invocation: tool definitions sit at prompt position 0 and invalidate
the entire prompt cache. One batched call per 5-hour window is the pacing rule --
enforced by the caller, not here."""

from __future__ import annotations

import json

from zecor.workers.base import Job, RateLimit, WorkerResult, resolve_tool, run_capped, run_verify

# route()'s model strings are logical -- the CLI wants its own aliases, which always
# resolve to the current release. Anything unmapped is passed through as-is.
_MODEL_ALIAS = {
    "claude-opus-5": "opus",
    "claude-sonnet-5": "sonnet",
    "claude-opus-4-6": "opus",
    "claude-sonnet-4-6": "sonnet",
}


class ClaudeWorker:
    tier_name = "T4"

    def run(self, job: Job) -> WorkerResult:
        cmd = [
            resolve_tool("claude"),
            "-p",
            job.prompt,
            "--output-format",
            "json",
            "--strict-mcp-config",  # ignore any discovered MCP servers
            "--mcp-config",
            '{"mcpServers":{}}',  # the CLI rejects bare '{}' -- needs the key
            "--permission-mode",
            "acceptEdits",  # headless: without this every Edit/Write is denied
        ]
        if job.model:
            cmd += ["--model", _MODEL_ALIAS.get(job.model, job.model)]

        rc, out, err, killed = run_capped(cmd, cwd=job.lane_dir, timeout_s=job.timeout_s)
        if killed:
            return WorkerResult("timeout", out, err)

        in_tok, out_tok, flags = _usage_from_json(out)
        if _looks_rate_limited(out, err):
            return WorkerResult(
                "ratelimited",
                out,
                err,
                in_tok,
                out_tok,
                rate_limit=RateLimit.rolling_5h("anthropic-pro"),
                flags=flags,
            )

        verify = run_verify(job)
        outcome = "pass" if verify.ok else "fail"
        return WorkerResult(outcome, out, err, in_tok, out_tok, verify=verify, flags=flags)


def _usage_from_json(raw: str) -> tuple[int, int, list[str]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0, 0, ["claude output was not JSON"]
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return 0, 0, []
    it = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
    ot = int(usage.get("output_tokens", 0))
    return it, ot, []


def _looks_rate_limited(out: str, err: str) -> bool:
    blob = (out + err).lower()
    return "rate limit" in blob or "usage limit" in blob or "429" in blob