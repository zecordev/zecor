# SPDX-License-Identifier: Apache-2.0
"""Layer 0 of the chatbot: a closed, no-AI command grammar.

Every operational verb is a keyword-anchored, filler-tolerant regex -- no model is
consulted, since a rate-limited network call must never sit between the operator and
"stop". `parse()` only classifies to `Parsed` or `Unknown`; the console dispatches the
result to `core.intents` or its own view state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Parsed:
    verb: str
    args: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Unknown:
    text: str


# id: 1-6 digits, optionally zero-padded. model/tier: a short token.
_ID = r"(?P<id>\d{1,6})"
_RULES: tuple[tuple[re.Pattern[str], str, tuple[str, ...]], ...] = (
    (re.compile(r"^\s*(?:help|\?|commands?)\s*$", re.I), "help", ()),
    # The two shaped forms come first: a free-text task description or a Move slug may
    # legitimately contain "stop", "run", "go" etc., and must not trip a control verb.
    (
        re.compile(
            r"^\s*(?:new|add|queue|create)\b.*?\b(?P<repo>[\w.\-]+)\s*[:=]\s*(?P<desc>.+)$",
            re.I,
        ),
        "add_inbox",
        ("repo", "desc"),
    ),
    (
        re.compile(
            r"^\s*(?:assign|move|put)\b\D*?" + _ID +
            r"\s*(?:to|under|into|on)?\s*(?P<move>[a-z0-9][a-z0-9-]*)\s*$",
            re.I,
        ),
        "assign",
        ("id", "move"),
    ),
    (re.compile(r"\b(?:stop|halt|pause|freeze)\b", re.I), "stop", ()),
    (re.compile(r"\b(?:go|resume|unpause|continue|unfreeze)\b", re.I), "go", ()),
    (re.compile(r"\b(?:plan|expand the inbox)\b", re.I), "plan", ()),
    (
        re.compile(
            r"\brun\b(?:.*?\buntil\s+(?P<until>\d{1,2}:\d{2}))?"
            r"(?:.*?\bmax\s+(?P<max>\d{1,4}))?",
            re.I,
        ),
        "run",
        ("until", "max"),
    ),
    (
        re.compile(
            r"\bretry\b\D*?" + _ID +
            r"(?:.*?\b(?:with|on|using|as)\s+(?P<model>opus|sonnet|haiku|[\w.\-]+))?"
            r"(?:.*?\b(?:tier)\s+(?P<tier>t0|t1|t2a|t2b|t3|t4|t5))?"
            r"(?:.*?\bnote\s+(?P<note>.+))?",
            re.I,
        ),
        "retry",
        ("id", "model", "tier", "note"),
    ),
    (re.compile(r"\b(?:un-?zombie|clear|kill|reap)\b\D*?" + _ID, re.I), "clear_zombie", ("id",)),
    (re.compile(r"^\s*undo\b", re.I), "undo", ()),
    (
        re.compile(r"^\s*focus\b\s*(?:on\s+)?(?P<repo>[\w.\-]+|all|off|clear)?\s*$", re.I),
        "focus",
        ("repo",),
    ),
    (re.compile(r"\b(?:show|card|inspect|details? of)\b\D*?" + _ID, re.I), "show", ("id",)),
    (re.compile(r"\b(?:blocked|problems|attention|whats? wrong|stuck)\b", re.I), "blocked", ()),
    (re.compile(r"\bzombies?\b", re.I), "zombies", ()),
    (re.compile(r"\b(?:digest|report|last night)\b", re.I), "digest", ()),
    (re.compile(r"\b(?:status|where are we|whats? up|overview|summary)\b", re.I), "status", ()),
)

VERBS = tuple(dict.fromkeys(verb for _, verb, _ in _RULES))


def parse(text: str) -> Parsed | Unknown:
    s = text.strip()
    if not s:
        return Unknown(text)
    for pattern, verb, names in _RULES:
        m = pattern.search(s)
        if not m:
            continue
        args = {n: m.group(n).strip() for n in names if m.group(n) is not None}
        if names and not args and verb not in ("run", "focus"):
            continue  # a verb that needs an arg but matched none -- keep looking
        return Parsed(verb, args)
    return Unknown(text)


HELP = (
    "stop | go | run [until HH:MM] [max N] | plan | retry <id> [with <model>] [tier tN] "
    "[note ...] | clear <id> | show <id> | assign <id> <move-slug> | blocked | zombies | "
    "digest | status | new <repo>: <what to do>"
)