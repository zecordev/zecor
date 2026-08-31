# SPDX-License-Identifier: Apache-2.0
"""Layer 1 of the chatbot: a local-model fallback for a sentence Layer 0 could not place.

`qwen3-4b-instruct` emits `{intent, slots}` under a strict JSON schema (LM Studio's
structured-output path); every slot is re-validated here against Layer 0's own rules. A
validation failure returns `Clarify` rather than a silent retry, and any network/model
failure returns `Unavailable`. No frontier call, ever -- this is a control plane, not a
chat.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from zecor import localmodel
from zecor.console.grammar import Parsed

# The verbs Layer 1 may resolve to -- the operational subset of Layer 0.
ACTION_VERBS = (
    "stop", "go", "run", "plan", "retry", "clear_zombie", "show", "assign",
    "add_inbox", "blocked", "zombies", "digest", "status", "help",
)
_TIERS = ("t0", "t1", "t2a", "t2b", "t3", "t4", "t5")

_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent"],
    "properties": {
        "intent": {"type": "string", "enum": [*ACTION_VERBS, "unknown"]},
        "id": {"type": "string"},
        "move": {"type": "string"},
        "model": {"type": "string"},
        "tier": {"type": "string"},
        "note": {"type": "string"},
        "repo": {"type": "string"},
        "desc": {"type": "string"},
        "until": {"type": "string"},
        "max": {"type": "string"},
    },
}

_SYSTEM = (
    "You translate ONE operator sentence into ONE zecor command for an autonomous "
    "software-build system. Reply with JSON only, matching the schema. `intent` must be "
    "one of the listed verbs, or \"unknown\" if you cannot tell. Fill only the slots the "
    "sentence actually specifies. Never invent a card id, repo, model or time. "
    "retry/clear_zombie/show need a numeric `id`. add_inbox needs `repo` and `desc` "
    "(desc = the operator's words, copied through). Do not write code."
)


@dataclass(frozen=True)
class Clarify:
    text: str
    options: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Unavailable:
    reason: str


def _prompt(text: str) -> str:
    return f"Sentence: {text!r}\nVerbs: {', '.join(ACTION_VERBS)}\nReply JSON."


def classify(text: str, *, repos: list[str] | None = None) -> Parsed | Clarify | Unavailable:
    try:
        comp = localmodel.complete(
            _prompt(text), system=_SYSTEM, json_schema=_SCHEMA,
            max_tokens=200, temperature=0.0, timeout_s=20.0,
        )
    except localmodel.PromptTooLong:
        return Unavailable("sentence too long for the local model")
    except localmodel.LocalModelError as e:
        return Unavailable(str(e))
    try:
        obj = json.loads(comp.text)
    except (json.JSONDecodeError, TypeError):
        return Clarify(text, _suggest(text))
    if not isinstance(obj, dict):
        return Clarify(text, _suggest(text))
    intent = str(obj.get("intent", "unknown"))
    if intent == "unknown" or intent not in ACTION_VERBS:
        return Clarify(text, _suggest(text))
    args = _clean(intent, obj, repos)
    if args is None:
        return Clarify(text, [intent, *(_suggest(text))][:3])
    return Parsed(intent, args)


def _clean(intent: str, obj: dict[str, object], repos: list[str] | None) -> dict[str, str] | None:
    def g(k: str) -> str:
        v = obj.get(k)
        return str(v).strip() if v not in (None, "") else ""

    out: dict[str, str] = {}
    if intent == "assign":
        cid = re.sub(r"\D", "", g("id"))
        slug = g("move").lower()
        if not cid or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            return None
        return {"id": cid, "move": slug}
    if intent in ("retry", "clear_zombie", "show"):
        cid = re.sub(r"\D", "", g("id"))
        if not cid:
            return None
        out["id"] = cid
        if intent == "retry":
            if g("model") and re.fullmatch(r"[\w.\-]{1,40}", g("model")):
                out["model"] = g("model")
            if g("tier").lower() in _TIERS:
                out["tier"] = g("tier").lower()
            if g("note"):
                out["note"] = g("note")
        return out
    if intent == "add_inbox":
        repo, desc = g("repo"), g("desc")
        if not repo or not desc:
            return None
        if repos and repo not in repos:
            return None  # a repo the model invented -- clarify instead
        return {"repo": repo, "desc": desc}
    if intent == "run":
        if re.fullmatch(r"\d{1,2}:\d{2}", g("until")):
            out["until"] = g("until")
        if g("max").isdigit():
            out["max"] = g("max")
        return out
    return out  # stop / go / plan / blocked / zombies / digest / status / help


_HINTS = {
    "retry": ("retry", "again", "rerun", "another model"),
    "stop": ("stop", "halt", "pause", "kill it"),
    "go": ("resume", "start", "continue"),
    "run": ("run", "work the queue", "go through"),
    "blocked": ("blocked", "stuck", "wrong", "failing"),
    "digest": ("digest", "report", "summary", "last night"),
    "status": ("status", "where", "how are we"),
    "add_inbox": ("build", "add", "new task", "fix"),
}


def _suggest(text: str) -> list[str]:
    low = text.lower()
    hits = [v for v, kws in _HINTS.items() if any(k in low for k in kws)]
    return hits[:3] or ["status", "blocked", "help"]