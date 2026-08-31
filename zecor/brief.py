# SPDX-License-Identifier: Apache-2.0
"""The context-firewall brief.

LM Studio's schema-constrained decoding should make a malformed brief impossible; this
module is the defensive re-check, plus the hard token cap -- truncate and flag, never
pass a fat brief through.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

MAX_BRIEF_TOKENS = 2000  # cap in the harness; ~4 chars/token heuristic
_CHARS_PER_TOKEN = 4

_REQUIRED = (
    "summary",
    "failures",
    "already_ruled_out",
    "files_worth_reading",
    "question_for_escalation",
)
_FAILURE_KEYS = ("test", "file", "expected", "actual", "first_divergence")


class BriefError(ValueError):
    """The brief did not conform -- the firewall has a hole; do not pass it through."""


@dataclass
class Failure:
    test: str
    file: str
    expected: str
    actual: str
    first_divergence: str


@dataclass
class Brief:
    summary: str
    failures: list[Failure]
    already_ruled_out: list[str]
    files_worth_reading: list[str]
    question_for_escalation: str | None
    truncated: bool = False
    flags: list[str] = field(default_factory=list)

    def names_a_decision(self) -> bool:
        """Escalate on `question_for_escalation` only when it names a decision, not a
        fact the model merely doesn't know (that is a retrieval miss)."""
        q = (self.question_for_escalation or "").lower()
        if not q:
            return False
        decision_markers = ("should ", "supposed to", "which of", "or ", "intended", "by design")
        return any(m in q for m in decision_markers)


def parse(raw: str) -> Brief:
    est_tokens = len(raw) // _CHARS_PER_TOKEN
    truncated = est_tokens > MAX_BRIEF_TOKENS
    flags: list[str] = []
    if truncated:
        flags.append(f"brief was ~{est_tokens} tokens, over the {MAX_BRIEF_TOKENS} cap")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BriefError(f"not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise BriefError("top level is not an object")

    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise BriefError(f"missing required keys: {missing}")

    failures: list[Failure] = []
    raw_failures = data["failures"]
    if not isinstance(raw_failures, list):
        raise BriefError("`failures` is not a list")
    for i, f in enumerate(raw_failures):
        if not isinstance(f, dict) or any(k not in f for k in _FAILURE_KEYS):
            raise BriefError(f"failures[{i}] missing keys {_FAILURE_KEYS}")
        failures.append(Failure(*(str(f[k]) for k in _FAILURE_KEYS)))

    q = data["question_for_escalation"]
    if q is not None and not isinstance(q, str):
        raise BriefError("`question_for_escalation` must be a string or null")

    return Brief(
        summary=str(data["summary"]),
        failures=failures,
        already_ruled_out=[
            str(x) for x in _as_list(data["already_ruled_out"], "already_ruled_out")
        ],
        files_worth_reading=[
            str(x) for x in _as_list(data["files_worth_reading"], "files_worth_reading")
        ],
        question_for_escalation=q,
        truncated=truncated,
        flags=flags,
    )


def load_schema(path: Path | None = None) -> dict[str, object]:
    p = path or Path(__file__).resolve().parent.parent / "prompts" / "brief.schema.json"
    parsed: dict[str, object] = json.loads(p.read_text())
    return parsed


def _as_list(v: object, name: str) -> list[object]:
    if not isinstance(v, list):
        raise BriefError(f"`{name}` is not a list")
    return v