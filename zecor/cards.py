# SPDX-License-Identifier: Apache-2.0
"""Task cards and the inbox.

Your whole input UI is one line in `queue/inbox.md`:

    - platform-toolkit: account badge still shows the old status after a change

`zecor plan` expands each line into `tasks/<id>.md` -- a card with intent, the verify
command that proves it, the files it may touch, and an explicit non-goal list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from zecor.task import Klass, Task

_INBOX_LINE = re.compile(r"^\s*-\s+(?P<repo>[A-Za-z0-9._-]+)\s*:\s*(?P<intent>.+?)\s*$")

# Every scalar key render()/parse() handles, plus the two list headers. A frontmatter
# key not in here is round-tripped through Card.extra rather than silently dropped.
_KNOWN_KEYS = frozenset(
    {
        "id", "repo", "klass", "state", "verify_cmd", "tag", "retry_after", "defer_count",
        "shape", "depends_on", "move", "fanout", "retry_tier", "retry_model", "retry_context",
        "retry_count",
        "rev", "multimodal_input",
        "deterministic_fix_available", "intent", "allow_files", "non_goals",
    }
)


@dataclass
class InboxItem:
    repo: str
    intent: str
    raw: str


def parse_inbox(text: str) -> list[InboxItem]:
    items: list[InboxItem] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _INBOX_LINE.match(line)
        if m:
            items.append(InboxItem(m.group("repo"), m.group("intent"), line.rstrip()))
    return items


@dataclass
class Card:
    id: str
    repo: str
    intent: str
    verify_cmd: str
    allow_files: list[str]
    non_goals: list[str] = field(default_factory=list)
    klass: Klass = Klass.BOUNDED
    tag: str | None = None
    state: str = "ready"  # ready | needs-human-design | blocked | landed | deferred | gate-broken
    retry_after: str | None = None  # ISO8601 UTC -- a deferred card stays out of the
    #                                 ready queue until this passes (no dry-window churn)
    defer_count: int = 0  # consecutive defers -- promoted to needs-human after a cap so a
    #                       task that can never route stops churning invisibly
    # routing inputs the planner sets so route()'s branches are live
    shape: str | None = None  # "bulk_mechanical" -> T2a fast-path
    multimodal_input: bool = False  # video/audio/PDF in the task -> T2b's monopoly
    deterministic_fix_available: bool = False  # a rules/ codemod matches -> T0
    depends_on: list[str] = field(default_factory=list)  # card ids that must land first
    #   (they touch the same files -- running concurrently makes conflicting branches)
    move: str | None = None  # roadmap membership -- the slug of the Move this card is under
    #   (upward pointer only; the Move/Campaign/Mission nodes live in roadmap/*.md)
    fanout: str | None = None  # shared id for the parallel cards of one requirement
    #   spread across client repos (zecor fanout) -- for the cross-repo parity check
    retry_tier: str | None = None  # `zecor retry --tier` -- force the first attempt's tier
    retry_model: str | None = None  # `zecor retry --model` -- force the model within it
    retry_context: str | None = None  # `zecor retry --note` -- extra constraint for the loop
    retry_count: int = 0  # how many times a human has re-queued this card
    rev: int = 0  # optimistic-concurrency counter -- core.cards.mutate() bumps it per
    #               write and refuses a write whose base rev has moved
    extra: dict[str, str] = field(default_factory=dict)  # unknown frontmatter keys, kept
    #   verbatim through parse->render so a field a newer writer added is never dropped

    def __post_init__(self) -> None:
        # `route()` matches tag == "hard" exactly; a hand-written `Hard` / ` hard `
        # must not silently route to the wrong tier.
        if self.tag:
            self.tag = self.tag.strip().lower() or None

    def to_task(self) -> Task:
        return Task(
            id=self.id,
            intent=self.intent,
            verify_cmd=self.verify_cmd,
            allow_files=list(self.allow_files),
            non_goals=list(self.non_goals),
            klass=self.klass,
            tag=self.tag,
            shape=self.shape,
            multimodal_input=self.multimodal_input,
            deterministic_fix_available=self.deterministic_fix_available,
        )

    def render(self) -> str:
        lines = [
            "---",
            f"id: {self.id}",
            f"repo: {self.repo}",
            f"klass: {self.klass.value}",
            f"state: {self.state}",
            f"verify_cmd: {self.verify_cmd}",
            f"rev: {self.rev}",
        ]
        if self.tag:
            lines.append(f"tag: {self.tag}")
        if self.retry_after:
            lines.append(f"retry_after: {self.retry_after}")
        if self.defer_count:
            lines.append(f"defer_count: {self.defer_count}")
        if self.shape:
            lines.append(f"shape: {self.shape}")
        if self.depends_on:
            lines.append(f"depends_on: {', '.join(self.depends_on)}")
        if self.move:
            lines.append(f"move: {self.move}")
        if self.fanout:
            lines.append(f"fanout: {self.fanout}")
        if self.retry_tier:
            lines.append(f"retry_tier: {self.retry_tier}")
        if self.retry_model:
            lines.append(f"retry_model: {self.retry_model}")
        if self.retry_context:
            lines.append(f"retry_context: {self.retry_context}")
        if self.retry_count:
            lines.append(f"retry_count: {self.retry_count}")
        if self.multimodal_input:
            lines.append("multimodal_input: true")
        if self.deterministic_fix_available:
            lines.append("deterministic_fix_available: true")
        for k, v in self.extra.items():
            if k not in _KNOWN_KEYS:
                lines.append(f"{k}: {v}")
        lines.append("allow_files:")
        lines += [f"  - {f}" for f in self.allow_files]
        lines.append("non_goals:")
        lines += [f"  - {g}" for g in self.non_goals]
        lines += ["---", "", f"# {self.intent}", ""]
        return "\n".join(lines)

    @classmethod
    def parse(cls, text: str) -> Card:
        fm, body = _split_frontmatter(text)
        allow = _list_field(fm, "allow_files")
        non_goals = _list_field(fm, "non_goals")
        intent = ""
        for line in body.splitlines():
            if line.startswith("# "):
                intent = line[2:].strip()
                break
        klass_raw = _scalar(fm, "klass", Klass.BOUNDED.value)
        return cls(
            id=_scalar(fm, "id", ""),
            repo=_scalar(fm, "repo", ""),
            intent=intent or _scalar(fm, "intent", ""),
            verify_cmd=_scalar(fm, "verify_cmd", ".zecor/verify.sh"),
            allow_files=allow,
            non_goals=non_goals,
            klass=Klass(klass_raw),
            tag=_scalar(fm, "tag", "") or None,
            state=_scalar(fm, "state", "ready"),
            retry_after=_scalar(fm, "retry_after", "") or None,
            defer_count=int(_scalar(fm, "defer_count", "0") or "0"),
            depends_on=[x.strip() for x in _scalar(fm, "depends_on", "").split(",") if x.strip()],
            move=_scalar(fm, "move", "") or None,
            fanout=_scalar(fm, "fanout", "") or None,
            retry_tier=_scalar(fm, "retry_tier", "") or None,
            retry_model=_scalar(fm, "retry_model", "") or None,
            retry_context=_scalar(fm, "retry_context", "") or None,
            retry_count=int(_scalar(fm, "retry_count", "0") or "0"),
            shape=_scalar(fm, "shape", "") or None,
            multimodal_input=_scalar(fm, "multimodal_input", "").lower() == "true",
            deterministic_fix_available=_scalar(fm, "deterministic_fix_available", "").lower()
            == "true",
            rev=int(_scalar(fm, "rev", "0") or "0"),
            extra=_extra_fields(fm),
        )

    def write(self, tasks_dir: Path) -> Path:
        path = tasks_dir / f"{self.id}.md"
        path.write_text(self.render())
        return path


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1], parts[2]


def _scalar(fm: str, key: str, default: str) -> str:
    for line in fm.splitlines():
        s = line.strip()
        if s.startswith(f"{key}:"):
            return s[len(key) + 1 :].strip()
    return default


def _extra_fields(fm: str) -> dict[str, str]:
    """Frontmatter scalar keys render()/parse() do not know about -- kept so a forward
    field is not lost on the next write. List items and indented lines are skipped."""
    out: dict[str, str] = {}
    for line in fm.splitlines():
        if not line.strip() or line[:1] in (" ", "\t") or line.lstrip().startswith("-"):
            continue
        m = re.match(r"([A-Za-z0-9_-]+):(.*)$", line.strip())
        if m and m.group(1) not in _KNOWN_KEYS:
            out[m.group(1)] = m.group(2).strip()
    return out


def _list_field(fm: str, key: str) -> list[str]:
    out: list[str] = []
    collecting = False
    for line in fm.splitlines():
        stripped = line.strip()
        if stripped == f"{key}:":
            collecting = True
            continue
        if collecting:
            if stripped.startswith("- "):
                out.append(stripped[2:].strip())
            elif stripped and not line.startswith(" "):
                break
    return out