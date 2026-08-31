# SPDX-License-Identifier: Apache-2.0
"""The Command Center invariant: every card-file write goes through zecor.core.

If this fails, some module started writing tasks/<id>.md directly again and the
serialized-writer / lease guarantees no longer hold for that path.
"""

from __future__ import annotations

import re
from pathlib import Path

FLEET = Path(__file__).resolve().parents[1] / "zecor"

# call shapes that persist a card file
_WRITE = re.compile(r"\.write\(\s*(config\.)?TASKS_DIR|\bcard\.write\(|\bCard\.write\(")


def test_no_card_writes_outside_core() -> None:
    offenders: list[str] = []
    for py in FLEET.rglob("*.py"):
        if py.parent.name == "core":
            continue
        for n, line in enumerate(py.read_text().splitlines(), 1):
            if _WRITE.search(line):
                offenders.append(f"{py.relative_to(FLEET.parent)}:{n}: {line.strip()}")
    assert not offenders, "card writes must go through zecor.core.cards:\n" + "\n".join(offenders)


def test_core_cards_is_the_only_flock_holder() -> None:
    # a second flock on tasks/.lock elsewhere would defeat the single-writer design
    hits: list[str] = []
    for py in FLEET.rglob("*.py"):
        if py.parent.name == "core":
            continue
        txt = py.read_text()
        if "tasks/.lock" in txt or "TASKS_DIR / \".lock\"" in txt:
            hits.append(str(py.relative_to(FLEET.parent)))
    assert not hits, f"tasks/.lock touched outside core: {hits}"