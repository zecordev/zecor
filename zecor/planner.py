# SPDX-License-Identifier: Apache-2.0
"""zecor plan -- expand inbox lines into task cards.

T1 drafts each card; `review_queue` then makes one batched frontier pass to catch a
mis-classification, an oversized card, or two cards in the wrong order. A unit of work
is <=3 files and <=150 lines; anything that can't be sliced that small goes to
`needs-human-design` instead of failing.
"""

from __future__ import annotations

import contextlib
import json
import re

from zecor.cards import Card, InboxItem
from zecor.config import MODEL_PRIMARY_CODER, UNIT_MAX_FILES
from zecor.frontier import NoFrontierAvailable, ask
from zecor.intake import classify
from zecor.jsonx import first_object
from zecor.localmodel import LocalModelError, complete
from zecor.repos import RepoNotFound, has_fleet_gate, resolve
from zecor.task import Klass

_SLICE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verify_cmd", "allow_files", "non_goals", "sliceable"],
    "properties": {
        "verify_cmd": {"type": "string"},
        "allow_files": {"type": "array", "items": {"type": "string"}, "maxItems": UNIT_MAX_FILES},
        "non_goals": {"type": "array", "items": {"type": "string"}},
        "sliceable": {"type": "boolean"},
    },
}

_SYSTEM = (
    "You turn a one-line task into a work card. Reply as JSON matching the schema.\n"
    "verify_cmd: the smallest command that would prove the task done -- prefer the repo's "
    "`.fleet/verify.sh`, or a narrower test invocation if you can name one.\n"
    "allow_files: at most 3 files this change may touch. Empty list if you cannot tell.\n"
    "non_goals: things a reader might assume are in scope but are not.\n"
    "sliceable: false if this cannot be done in <=3 files and <=150 lines."
)


def plan_item(item: InboxItem, next_id: str) -> Card:
    c = classify(item.repo, item.intent)
    # A flagged classification means the local model was down or gave an unparseable
    # label -- we have NO reliable class. Fail toward a human, not toward the
    # dispatchable default: an unclassified judgment question must not slip into the
    # run as BOUNDED.
    klass = Klass.JUDGMENT if c.flagged else c.klass

    has_gate = False
    repo_dir = None
    with contextlib.suppress(RepoNotFound):
        repo_dir = resolve(item.repo)
        has_gate = has_fleet_gate(repo_dir)
    default_verify = "./.zecor/verify.sh" if has_gate else ".zecor/verify.sh"

    verify_cmd, allow, non_goals, sliceable = default_verify, [], [], True
    if klass is not Klass.JUDGMENT:
        try:
            out = complete(
                f"repo: {item.repo}\ntask: {item.intent}\n\ncard:",
                model=MODEL_PRIMARY_CODER,
                system=_SYSTEM,
                max_tokens=400,
                json_schema=_SLICE_SCHEMA,
            )
            data = json.loads(out.text)
            allow = [_strip_repo_prefix(str(f), item.repo) for f in data.get("allow_files", [])]
            allow = [f for f in allow if f][:UNIT_MAX_FILES]
            # The local model guesses paths that don't exist (wrong basename, wrong
            # nesting) -- an allow-list naming no real file is worse than an empty one,
            # since it makes every in-scope edit look out-of-scope.
            if repo_dir is not None:
                allow = [f for f in allow if (repo_dir / f).exists()]
            non_goals = [str(g) for g in data.get("non_goals", [])]
            sliceable = bool(data.get("sliceable", True))
            # The gate command is not the local model's to write -- it composes bad paths
            # (repo-name prefixes, wrong cwd). Trust it only when there is no repo gate
            # AND it names a test runner, not a shell one-liner.
            if not has_gate:
                cand = str(data.get("verify_cmd") or "").strip()
                if cand and _looks_like_a_test_runner(cand):
                    verify_cmd = cand
        except (LocalModelError, json.JSONDecodeError, ValueError):
            pass  # keep the defaults; the card file is yours to fix

    # Retrieval: the local model often leaves allow_files empty or short. Fill from the
    # deterministic symbol index + an embedding rerank -- real paths only.
    if repo_dir is not None and len(allow) < UNIT_MAX_FILES:
        try:
            from zecor.retrieval import suggest

            for f in suggest(repo_dir, item.intent, k=UNIT_MAX_FILES):
                if f not in allow and (repo_dir / f).exists():
                    allow.append(f)
            allow = allow[:UNIT_MAX_FILES]
        except (OSError, ValueError):
            pass

    state = "ready"
    if klass is Klass.JUDGMENT or not sliceable:
        state = "needs-human-design"

    det = False
    if klass is not Klass.JUDGMENT:
        with contextlib.suppress(OSError):
            from zecor.t0 import has_rule_for

            det = has_rule_for(item.repo, item.intent)

    return Card(
        id=next_id,
        repo=item.repo,
        intent=item.intent,
        verify_cmd=verify_cmd,
        allow_files=allow,
        non_goals=non_goals,
        klass=klass,
        state=state,
        multimodal_input=_has_multimodal(item.intent),
        shape=_shape_of(item.intent),
        deterministic_fix_available=det,
    )


_MULTIMODAL = re.compile(
    r"\b(screenshot|screen ?recording|video|audio|mp4|mov|wav|mp3|\.pdf\b|"
    r"mockup|figma|design ?file|image attached|see the (image|picture|video))\b",
    re.I,
)
_BULK = re.compile(
    r"\b(all (the )?(files|components|call sites|occurrences)|every (file|module|"
    r"import)|across the (repo|codebase)|sweep|bulk|codemod|rename .* everywhere|"
    r"repo-wide|project-wide)\b",
    re.I,
)


def _has_multimodal(intent: str) -> bool:
    return bool(_MULTIMODAL.search(intent))


def _shape_of(intent: str) -> str | None:
    return "bulk_mechanical" if _BULK.search(intent) else None


def review_queue(cards: list[Card]) -> tuple[list[Card], str]:
    """One batched frontier pass over the whole draft queue. Returns the cards
    (possibly adjusted) and a note. A no-op with <2 cards or no frontier tier."""
    if len(cards) < 2:
        return cards, ""
    listing = "\n".join(
        f"{c.id}. [{c.klass.value}] {c.repo}: {c.intent}\n"
        f"     verify: {c.verify_cmd}  files: {', '.join(c.allow_files) or '(any)'}"
        for c in cards
    )
    prompt = (
        "These are draft task cards for an autonomous coding engine. Each task must change "
        "<=3 files / <=150 lines and have an objective verify command. Review the SET:\n"
        "- wrong class? (mechanical/bounded/unbounded/judgment)\n"
        "- too big to be one card? (mark split)\n"
        "- depends on another card that lands later? (mark reorder)\n"
        "- verify command obviously wrong?\n\n"
        f"{listing}\n\n"
        'Reply JSON: {"adjustments": [{"id": "0001", "klass": "bounded"|null, '
        '"verify_cmd": "..."|null, "flag": "split"|"reorder"|null, "why": "..."}]}. '
        "Empty adjustments list if the queue is fine."
    )
    try:
        text, tier = ask(prompt, timeout_s=240.0)
        adj = first_object(text).get("adjustments", [])
    except (NoFrontierAvailable, ValueError):
        return cards, "queue review skipped (no frontier / bad reply)"
    if not isinstance(adj, list):
        return cards, "queue review: no adjustments"

    by_id = {c.id: c for c in cards}
    notes: list[str] = []
    for a in adj:
        if not isinstance(a, dict):
            continue
        c = by_id.get(str(a.get("id", "")))
        if c is None:
            continue
        new_klass = a.get("klass")
        if isinstance(new_klass, str) and new_klass in {k.value for k in Klass}:
            c.klass = Klass(new_klass)
        new_verify = a.get("verify_cmd")
        if isinstance(new_verify, str) and new_verify.strip():
            c.verify_cmd = new_verify.strip()
        flag = a.get("flag")
        if flag in ("split", "reorder"):
            c.state = "needs-human-design"
            notes.append(f"{c.id}: {flag} -- {a.get('why', '')}")
        if c.klass is Klass.JUDGMENT:
            c.state = "needs-human-design"

    dep_notes = _link_file_overlaps(cards)
    summary = f"queue review via {tier}: {len(notes)} flagged" + (
        " (" + "; ".join(notes) + ")" if notes else ""
    )
    if dep_notes:
        summary += f"; {len(dep_notes)} serialised ({'; '.join(dep_notes)})"
    return cards, summary


def _link_file_overlaps(cards: list[Card]) -> list[str]:
    """Two same-repo cards with an overlapping allow-list produce conflicting branches
    if they run in parallel. Make the later one depend on the earlier (by id order)."""
    notes: list[str] = []
    ordered = sorted(cards, key=lambda c: c.id)
    for i, later in enumerate(ordered):
        if not later.allow_files:
            continue
        for earlier in ordered[:i]:
            if earlier.repo != later.repo or not earlier.allow_files:
                continue
            shared = set(earlier.allow_files) & set(later.allow_files)
            if shared and earlier.id not in later.depends_on:
                later.depends_on.append(earlier.id)
                notes.append(f"{later.id} after {earlier.id} ({', '.join(sorted(shared))})")
    return notes


def _strip_repo_prefix(path: str, repo: str) -> str:
    p = path.strip().lstrip("./")
    for pre in (f"{repo}/", f"{repo.replace('-', '_')}/"):
        if p.startswith(pre):
            return p[len(pre) :]
    return p


_TEST_RUNNERS = ("pytest", "vitest", "jest", "go test", "cargo test", "npm test", "pnpm test")


def _looks_like_a_test_runner(cmd: str) -> bool:
    c = cmd.strip().lower()
    return any(c.startswith(r) or f" {r} " in c for r in _TEST_RUNNERS)