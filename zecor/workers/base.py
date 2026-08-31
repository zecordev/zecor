# SPDX-License-Identifier: Apache-2.0
"""Worker plumbing: prompt in, patch + JSON result out, run inside `lanes/<id>/`,
wall-clock capped, killed on timeout. The dispatcher does not care which one ran. MCP
servers are disabled on every subprocess invocation -- each injects tool definitions
into every message, consumes quota, and (on Claude) sits at prompt position 0, so it
invalidates the entire prompt cache."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

TERM_GRACE_S = 5.0  # SIGTERM, then SIGKILL after this


@dataclass
class RateLimit:
    window: str  # '5h' | 'daily' | 'weekly'
    used_now: int
    resets_at: str  # ISO8601 -- the ledger treats the tier as dry until this instant

    @staticmethod
    def _in(hours: float) -> str:
        return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()

    @classmethod
    def rolling_5h(cls, _vendor: str) -> RateLimit:
        return cls("5h", 0, cls._in(5))

    @classmethod
    def daily(cls, _vendor: str) -> RateLimit:
        return cls("daily", 0, cls._in(24))

    @classmethod
    def weekly(cls, _vendor: str) -> RateLimit:
        return cls("weekly", 0, cls._in(24 * 7))


@dataclass
class VerifyRun:
    exit: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit == 0


@dataclass
class WorkerResult:
    outcome: str  # pass | fail | timeout | refused | ratelimited  (matches the ledger)
    stdout: str = ""
    stderr: str = ""
    in_tokens: int = 0
    out_tokens: int = 0
    rate_limit: RateLimit | None = None
    verify: VerifyRun | None = None  # set by appliers (T1/T3/T4); None for reader tiers
    flags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "pass"


@dataclass
class Job:
    prompt: str
    lane_dir: Path
    model: str | None
    timeout_s: float
    verify_cmd: str  # run after the edit to decide pass/fail
    readonly: tuple[str, ...] = ()  # paths the worker may read but not edit (test-first)
    repo: str = ""  # card.repo -- T0 uses it to pick which active rules apply
    intent: str = ""  # card.intent -- likewise (a rule may pin an intent regex)


class Worker(Protocol):
    tier_name: str

    def run(self, job: Job) -> WorkerResult: ...


def resolve_tool(name: str) -> str:
    """Absolute path to a vendor CLI, resolved against the engine's own PATH, or `name`
    unchanged if not found. `run_capped`'s hardened-mode env pins a minimal PATH that
    would not otherwise find a tool under `~/.local/bin`, Homebrew, npm-global, etc.;
    resolving here is safe since the engine invokes these directly, not lane code."""
    import shutil

    return shutil.which(name) or name


def run_capped(
    cmd: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    stdin: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str, bool]:
    """Run a subprocess in its own process group, capped, SIGKILL on overrun. Returns
    (returncode, stdout, stderr, killed_for_timeout); in `hardened` mode the argv is
    checked against `zecor.toolgate` first, and a refusal comes back as exit 126 with
    the reason on stderr, before anything runs."""
    from zecor import modes, toolgate

    gate = toolgate.check(cmd)
    if not gate.allowed:
        return 126, "", f"zecor: command refused in hardened mode -- {gate.reason}", False

    env = _child_env(extra_env, hardened=modes.shell_is_allow_listed())

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,  # own process group, so we can kill the whole tree
        )
    except FileNotFoundError:
        # `cmd[0]` isn't installed -- exit 127 matches the shell's own "command not
        # found" and every existing 126/127 check (baseline, full-suite regression)
        # already treats it as "gate can't run" rather than a model failure.
        return 127, "", f"zecor: '{cmd[0]}' not found on PATH", False
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout_s)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return 124, out, err, True


# Credentials and code-loader variables a lane must never inherit.
_STRIP_ALWAYS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_METERED_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
    "NPM_TOKEN", "HF_TOKEN", "SLACK_TOKEN",
)
_LOADER_VARS = (
    "PYTHONSTARTUP", "PYTHONPATH", "NODE_OPTIONS", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
)
# A minimal, trusted environment for hardened-mode lanes.
_HARDENED_KEEP = (
    "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR", "TERM", "SSL_CERT_FILE",
)
_HARDENED_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _child_env(extra_env: dict[str, str] | None, *, hardened: bool) -> dict[str, str]:
    if hardened:
        env = {k: os.environ[k] for k in _HARDENED_KEEP if k in os.environ}
        env["PATH"] = _HARDENED_PATH
        venv = os.environ.get("VIRTUAL_ENV")
        if venv:
            env["PATH"] = f"{venv}/bin:{env['PATH']}"
            env["VIRTUAL_ENV"] = venv
    else:
        env = dict(os.environ)
    for k in (*_STRIP_ALWAYS, *_LOADER_VARS):
        env.pop(k, None)
    if extra_env:
        # extra_env must not smuggle a stripped credential or a loader var back in
        for k, v in extra_env.items():
            if k in _STRIP_ALWAYS or k in _LOADER_VARS:
                continue
            env[k] = v
    return env


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def run_verify(job: Job) -> VerifyRun:
    """Deterministic, no model involved -- exit code is the sole definition of done. In
    an isolation mode the command is wrapped by `zecor-sandbox` so the repo's own
    (attacker-influenced) test suite cannot write outside the lane or reach the net."""
    from zecor import sandbox

    argv = ["/bin/sh", "-c", job.verify_cmd]
    argv = sandbox.wrap(argv, workdir=job.lane_dir)
    rc, out, err, killed = run_capped(argv, cwd=job.lane_dir, timeout_s=job.timeout_s)
    return VerifyRun(124 if killed else rc, out, err)