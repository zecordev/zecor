# SPDX-License-Identifier: Apache-2.0
"""The roadmap tree -- Command Center build step 6.

Four levels: Mission (why, quarters) > Campaign (one shippable outcome, weeks) > Move
(one landable slice, days) > Card (one dispatch). Mission/Campaign/Move live in
`roadmap/<mission>.md` as nested markdown headings; a card joins by an upward
`move: <slug>` frontmatter field. No status is stored -- `rollup()` recomputes a
node's state as a fold over its leaf cards every time it is read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from zecor import config
from zecor.core.view import CardView

# Worst first. A node's rollup is the worst state among the cards beneath it.
_LATTICE = (
    "gate-broken",
    "blocked",
    "needs-human-design",
    "unreadable",
    "deferred",
    "unplanned",  # a Move/Campaign/Mission with no cards under it
    "in-progress",  # some cards landed, others not
    "ready",  # cards exist, none landed yet, none in trouble
    "landed",  # every card beneath is landed
)


def rank(state: str) -> int:
    try:
        return _LATTICE.index(state)
    except ValueError:
        return len(_LATTICE)


_HEADING = re.compile(r"^(#{1,3})\s+(?:(Mission|Campaign|Move)\s*:\s*)?(.+?)\s*$")
_SLUG_TAG = re.compile(r"\{slug:\s*([a-z0-9][a-z0-9-]*)\s*\}\s*$", re.I)
_LEVEL = {1: "mission", 2: "campaign", 3: "move"}


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return s or "untitled"


@dataclass
class Node:
    kind: str  # mission | campaign | move
    slug: str
    title: str
    body: str = ""
    children: list[Node] = field(default_factory=list)


def _parse_file(text: str) -> list[Node]:
    roots: list[Node] = []
    stack: list[Node] = []
    body_lines: list[str] = []

    def flush() -> None:
        if stack and body_lines:
            stack[-1].body = "\n".join(body_lines).strip()
        body_lines.clear()

    for line in text.splitlines():
        m = _HEADING.match(line)
        if not m:
            body_lines.append(line)
            continue
        flush()
        depth = len(m.group(1))
        kind = (m.group(2) or _LEVEL.get(depth, "move")).lower()
        title = m.group(3)
        slug_m = _SLUG_TAG.search(title)
        if slug_m:
            slug = slug_m.group(1).lower()
            title = title[: slug_m.start()].strip()
        else:
            slug = slugify(title)
        node = Node(kind=kind, slug=slug, title=title)
        stack[:] = stack[: depth - 1]
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    flush()
    return roots


def _is_roadmap(text: str) -> bool:
    """True iff the first markdown heading is `# Mission: ...` -- a design doc whose
    `# Mission:` appears later in the text is not a roadmap."""
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            return m.group(2) == "Mission" and m.group(1) == "#"
    return False


def load() -> list[Node]:
    """Every `roadmap/*.md` whose first heading is `# Mission: ...`, parsed into trees.
    A plain doc under `roadmap/` (a design plan, notes) is skipped. Missing dir -> []."""
    root = config.ZECOR_ROOT / "roadmap"
    try:
        files = sorted(root.glob("*.md"))
    except OSError:
        return []
    out: list[Node] = []
    for f in files:
        try:
            text = f.read_text()
        except OSError:
            continue
        if _is_roadmap(text):
            out.extend(_parse_file(text))
    return out


@dataclass(frozen=True)
class Rolled:
    kind: str
    slug: str
    title: str
    status: str
    landed: int
    total: int
    oldest_mtime: float  # 0.0 if no cards
    children: tuple[Rolled, ...] = ()
    cards: tuple[str, ...] = ()  # card ids directly under a Move


def _fold(states: list[str]) -> str:
    """Worst real state wins; all-landed -> landed; all-empty -> unplanned; else
    in-progress if anything landed, otherwise ready."""
    real = [s for s in states if s != "unplanned"]
    if not real:
        return "unplanned"
    if all(s == "landed" for s in real):
        return "landed"
    trouble = [s for s in real if s in _LATTICE[: _LATTICE.index("unplanned")]]
    if trouble:
        return min(trouble, key=rank)
    if any(s == "landed" for s in real):
        return "in-progress"
    return "ready"


def _roll_move(node: Node, by_move: dict[str, list[CardView]]) -> Rolled:
    cards = by_move.get(node.slug, [])
    states = [c.state for c in cards]
    return Rolled(
        kind="move",
        slug=node.slug,
        title=node.title,
        status=_fold(states),
        landed=sum(1 for c in cards if c.state == "landed"),
        total=len(cards),
        oldest_mtime=min((c.mtime for c in cards), default=0.0),
        cards=tuple(c.id for c in cards),
    )


def _roll_parent(node: Node, kids: list[Rolled]) -> Rolled:
    return Rolled(
        kind=node.kind,
        slug=node.slug,
        title=node.title,
        status=_fold([k.status for k in kids]) if kids else "unplanned",
        landed=sum(k.landed for k in kids),
        total=sum(k.total for k in kids),
        oldest_mtime=min((k.oldest_mtime for k in kids if k.oldest_mtime), default=0.0),
        children=tuple(kids),
    )


def _roll(node: Node, by_move: dict[str, list[CardView]]) -> Rolled:
    if node.kind == "move" or not node.children:
        base = _roll_move(node, by_move)
        by_move.pop(node.slug, None)  # a card belongs to one Move; a duplicate slug
        # elsewhere in document order rolls up empty, not twice
        if node.children:  # a Move with sub-headings: fold them in too
            kids = [_roll(c, by_move) for c in node.children]
            return _roll_parent(node, [base, *kids])
        return base
    kids = [_roll(c, by_move) for c in node.children]
    return _roll_parent(node, kids)


def rollup(missions: list[Node], cards: list[CardView]) -> list[Rolled]:
    by_move: dict[str, list[CardView]] = {}
    for c in cards:
        if c.move:
            by_move.setdefault(c.move, []).append(c)
    # one shared dict, consumed by _roll: the first Move (mission then document order)
    # to claim a slug gets its cards; a duplicate slug anywhere rolls up empty.
    return [_roll(m, by_move) for m in missions]


def orphan_cards(missions: list[Node], cards: list[CardView]) -> list[str]:
    """Card ids whose `move:` slug names no *Move* in the tree (or is unset). A slug that
    matches a Campaign or Mission does not count -- `move:` targets Moves only."""
    known: set[str] = set()

    def walk(n: Node) -> None:
        if n.kind == "move":
            known.add(n.slug)
        for c in n.children:
            walk(c)

    for m in missions:
        walk(m)
    return [c.id for c in cards if not c.move or c.move not in known]