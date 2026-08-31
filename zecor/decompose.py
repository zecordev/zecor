# SPDX-License-Identifier: Apache-2.0
"""zecor decompose -- a requirements doc becomes a sliced backlog.

A long doc is a project, not a task: this turns it into candidate task lines written
into the inbox as commented-out proposals for you to ratify. Architecture decisions
stay out -- Zecor only takes work with an exit code.
"""

from __future__ import annotations

from dataclasses import dataclass

from zecor.config import UNIT_MAX_FILES, UNIT_MAX_LINES
from zecor.frontier import ask
from zecor.jsonx import first_object

_PROMPT = f"""You are decomposing a project requirements document into a backlog of
independently landable tasks for an automated coding engine.

Hard rules:
- Each task changes <= {UNIT_MAX_FILES} files and <= {UNIT_MAX_LINES} lines.
- Each task must have an objective acceptance check -- a test or command that would
  pass only when it is done. If you cannot name one, the task is not ready; mark it.
- Order tasks so each builds on landed ones. No task may depend on an unlanded one.
- Leave architecture / "should we" decisions OUT. List them separately as `open_questions`.

Reply with ONLY this JSON (no prose):
{{"repo": "<the target repo name>",
  "tasks": [{{"intent": "<one sentence, imperative>", "check": "<the acceptance check>",
             "ready": true}}],
  "open_questions": ["<decision that needs a human>"]}}

--- REQUIREMENTS DOCUMENT ---
"""


@dataclass
class Decomposition:
    repo: str
    tasks: list[dict[str, object]]
    open_questions: list[str]
    tier: str
    raw: str


def decompose(doc_text: str, *, source: str = "requirements-doc") -> Decomposition:
    from zecor import modes, untrusted

    body = doc_text.strip()
    if modes.treat_external_input_as_hostile():
        body = untrusted.wrap(source, body)
    text, tier = ask(_PROMPT + body + "\n--- END ---\n")
    data = first_object(text)
    repo = str(data.get("repo", "")).strip() or "UNKNOWN-REPO"
    raw_tasks = data.get("tasks", [])
    tasks = [t for t in raw_tasks if isinstance(t, dict)] if isinstance(raw_tasks, list) else []
    raw_oq = data.get("open_questions", [])
    oq = [str(q) for q in raw_oq] if isinstance(raw_oq, list) else []
    return Decomposition(repo=repo, tasks=tasks, open_questions=oq, tier=tier, raw=text)


def to_inbox_block(d: Decomposition, source: str) -> str:
    lines = [
        "",
        f"# ==== proposed from {source} via {d.tier} -- review, then uncomment ====",
    ]
    for t in d.tasks:
        ready = bool(t.get("ready", True))
        mark = "" if ready else "  (NOT READY: no acceptance check)"
        intent = str(t.get("intent", "")).strip()
        lines.append(f"# - {d.repo}: {intent}{mark}")
        check = str(t.get("check", "")).strip()
        if check:
            lines.append(f"#     check: {check}")
    if d.open_questions:
        lines.append("# open questions for you (not zecor work):")
        lines += [f"#   - {q}" for q in d.open_questions]
    lines.append("# ==== end proposal ====")
    return "\n".join(lines) + "\n"