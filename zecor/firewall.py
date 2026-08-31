# SPDX-License-Identifier: Apache-2.0
"""The context firewall.

No frontier model ever sees a raw artifact -- the local model turns it into a
fixed-schema brief first. `zecor` is the only thing that talks to the vendor CLIs; if a
raw log is ever piped straight into `claude -p`, the firewall has a hole.
"""

from __future__ import annotations

from zecor.brief import Brief, BriefError, load_schema, parse
from zecor.config import MODEL_REASONER
from zecor.localmodel import LocalModelError, complete

_SYSTEM = (
    "You are a context firewall. Read the raw artifact below and emit a JSON brief that a "
    "senior engineer could act on without seeing the artifact. Summarize; do not editorialize. "
    "Every `first_divergence` is a path:line plus the mechanism in one clause. "
    "`question_for_escalation` is non-null ONLY if it names a DECISION the code cannot settle "
    "(not a fact you merely don't know). Keep it under 2000 tokens."
)

_MAX_ARTIFACT_CHARS = 24000  # local prompt cap is 8K tokens; leave room for system + schema


def make_brief(artifact: str, *, context: str = "", model: str = MODEL_REASONER) -> Brief:
    """Turn a raw artifact into a schema-valid Brief. Raises on an unreachable local model
    or an unparseable result -- the caller decides whether to escalate raw or defer."""
    clipped = artifact[:_MAX_ARTIFACT_CHARS]
    if len(artifact) > _MAX_ARTIFACT_CHARS:
        clipped += "\n...[artifact clipped by the firewall]..."

    prompt = f"{context}\n\n--- RAW ARTIFACT ---\n{clipped}\n--- END ---\n\nJSON brief:"
    schema = load_schema()
    try:
        out = complete(
            prompt,
            model=model,
            system=_SYSTEM,
            max_tokens=1200,
            temperature=0.0,
            json_schema=schema,
        )
    except LocalModelError:
        raise
    try:
        return parse(out.text)
    except BriefError as e:
        raise BriefError(
            f"firewall produced a non-conforming brief: {e}\n---\n{out.text[:800]}"
        ) from e


def brief_to_prompt(brief: Brief, task_intent: str) -> str:
    """Render a Brief as the only thing a frontier tier sees for this escalation."""
    lines = [
        f"Task: {task_intent}",
        "",
        f"Summary: {brief.summary}",
        "",
        "Failures:",
    ]
    for f in brief.failures:
        lines.append(
            f"  - {f.test} ({f.file}): expected {f.expected}, got {f.actual}. "
            f"First divergence: {f.first_divergence}"
        )
    if brief.already_ruled_out:
        lines += ["", "Already ruled out: " + "; ".join(brief.already_ruled_out)]
    if brief.files_worth_reading:
        lines += ["", "Files worth reading: " + ", ".join(brief.files_worth_reading)]
    if brief.question_for_escalation:
        lines += ["", f"Open question: {brief.question_for_escalation}"]
    if brief.flags:
        lines += ["", "Flags: " + "; ".join(brief.flags)]
    return "\n".join(lines)