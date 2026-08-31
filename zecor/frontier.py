# SPDX-License-Identifier: Apache-2.0
"""One-shot frontier text calls that are not lane work: decomposing a requirements doc
into a sliced backlog, reviewing the night's queue. Not for applying a patch -- that
goes through the lane workers. MCP is disabled here for the same reason as everywhere:
tool definitions blow the prompt cache."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from zecor.workers.base import resolve_tool, run_capped


class NoFrontierAvailable(RuntimeError):
    pass


def ask(prompt: str, *, timeout_s: float = 300.0) -> tuple[str, str]:
    """Return (text, which_tier). Tries Claude, then Codex."""
    with tempfile.TemporaryDirectory(prefix="zecor-ask-") as td:
        cwd = Path(td)
        text = _try_claude(prompt, cwd, timeout_s)
        if text is not None:
            return text, "t4"
        text = _try_codex(prompt, cwd, timeout_s)
        if text is not None:
            return text, "t3"
    raise NoFrontierAvailable("neither `claude` nor `codex` produced a result")


def _try_claude(prompt: str, cwd: Path, timeout_s: float) -> str | None:
    rc, out, _err, killed = run_capped(
        [
            resolve_tool("claude"),
            "-p",
            prompt,
            "--output-format",
            "json",
            "--strict-mcp-config",
            "--mcp-config",
            "{}",
        ],
        cwd=cwd,
        timeout_s=timeout_s,
    )
    if killed or rc != 0:
        return None
    try:
        data = json.loads(out)
        result = data.get("result")
        return str(result) if result else None
    except json.JSONDecodeError:
        return out or None


def _try_codex(prompt: str, cwd: Path, timeout_s: float) -> str | None:
    rc, out, _err, killed = run_capped(
        [resolve_tool("codex"), "exec", "--json", "--skip-git-repo-check", prompt],
        cwd=cwd,
        timeout_s=timeout_s,
    )
    if killed or rc != 0:
        return None
    # codex --json streams events; the assistant text rides `type":"item.completed"` /
    # `message` events. Fall back to the raw stdout.
    chunks: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = evt.get("message") or evt.get("text")
        if isinstance(msg, str):
            chunks.append(msg)
    return "\n".join(chunks) if chunks else (out or None)