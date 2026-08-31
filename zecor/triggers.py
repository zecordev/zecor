# SPDX-License-Identifier: Apache-2.0
"""The nine escalation triggers, each a pure function over artifacts the model cannot
fake -- never its self-assessed confidence, the least reliable signal available.
Returns a reason string when it fires, else None."""

from __future__ import annotations

from dataclasses import dataclass, field

from zecor.route import LOCAL_PROMPT_CAP
from zecor.task import Task

# BENCH.md: primary coder does ~38 tok/s after a ~14s read, so a well-sliced task is
# ~2-3 min of model time; start tighter and let loop_stall data tune it.
WALL_CLOCK_CAP_S = 900
MAX_LINES_DEFAULT = 150


@dataclass
class Attempt:
    """What the harness captured about one local attempt -- all externally observed."""

    n: int
    verify_exit: int  # .fleet/verify.sh exit code
    failing_assertions: frozenset[str]
    diff_hash: str
    touched_files: frozenset[str]
    lines_changed: int
    wall_s: float
    prompt_tokens: int
    unresolved_symbol: str | None = None  # aider repo-map + LSP could not resolve this
    question_for_escalation: str | None = None  # from the task brief
    names_a_decision: bool = False  # names a decision, not a missing fact
    green_before: frozenset[str] = field(default_factory=frozenset)  # tests green at task start


def gate_still_red(cur: Attempt, prev: Attempt | None, task: Task) -> str | None:
    if prev is not None and cur.verify_exit != 0 and prev.verify_exit != 0:
        return "gate red for 2 consecutive attempts"
    return None


def no_progress(cur: Attempt, prev: Attempt | None, task: Task) -> str | None:
    if (
        prev is not None
        and cur.verify_exit != 0
        and cur.failing_assertions == prev.failing_assertions
        and len(cur.failing_assertions) > 0
    ):
        return "same failing assertion set as the previous attempt"
    return None


def thrash(cur: Attempt, prev: Attempt | None, task: Task) -> str | None:
    if prev is not None and cur.diff_hash == prev.diff_hash and cur.diff_hash != "":
        return "diff duplicates the previous attempt"
    return None


def new_breakage(cur: Attempt, prev: Attempt | None, task: Task) -> str | None:
    broke = cur.green_before & cur.failing_assertions
    if broke:
        return f"broke {len(broke)} previously-green test(s): {sorted(broke)[:3]}"
    return None


def scope_creep(cur: Attempt, prev: Attempt | None, task: Task) -> str | None:
    allow = frozenset(task.allow_files)
    outside = cur.touched_files - allow
    if outside:
        return f"touched {len(outside)} file(s) outside the allow-list: {sorted(outside)[:3]}"
    ceiling = task.max_lines or MAX_LINES_DEFAULT
    if cur.lines_changed > ceiling:
        return f"changed {cur.lines_changed} lines, over the {ceiling}-line ceiling"
    return None


def context_pressure(cur: Attempt, prev: Attempt | None, task: Task) -> str | None:
    if cur.prompt_tokens > LOCAL_PROMPT_CAP:
        return f"needs {cur.prompt_tokens} prompt tokens, over the {LOCAL_PROMPT_CAP} local cap"
    return None


def loop_stall(cur: Attempt, prev: Attempt | None, task: Task) -> str | None:
    if cur.wall_s > WALL_CLOCK_CAP_S:
        return f"attempt ran {cur.wall_s:.0f}s, over the {WALL_CLOCK_CAP_S}s cap"
    return None


def retrieval_miss(cur: Attempt, prev: Attempt | None, task: Task) -> str | None:
    if cur.unresolved_symbol:
        # A fact the model doesn't know is a retrieval failure, not an escalation.
        return f"retrieval cannot resolve symbol {cur.unresolved_symbol!r} (route to T0 retrieval)"
    return None


def explicit_unknown(cur: Attempt, prev: Attempt | None, task: Task) -> str | None:
    if cur.question_for_escalation and cur.names_a_decision:
        return f"brief names a decision the code cannot settle: {cur.question_for_escalation}"
    return None


ALL_TRIGGERS = (
    gate_still_red,
    no_progress,
    thrash,
    new_breakage,
    scope_creep,
    context_pressure,
    loop_stall,
    retrieval_miss,
    explicit_unknown,
)


def evaluate(cur: Attempt, prev: Attempt | None, task: Task) -> list[str]:
    """All reasons the harness should escalate after this attempt. Empty means keep going."""
    fired: list[str] = []
    for trig in ALL_TRIGGERS:
        reason = trig(cur, prev, task)
        if reason is not None:
            fired.append(f"{trig.__name__}: {reason}")
    return fired