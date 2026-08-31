# SPDX-License-Identifier: Apache-2.0
"""The task card. `zecor plan` expands one line of `queue/inbox.md` into one of these --
the dispatcher never invents fields; a card that cannot state its verify command isn't
ready."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Klass(enum.Enum):
    """Decided at intake, not by discovering failure."""

    MECHANICAL = "mechanical"  # a deterministic rule could almost express it -> T0 then T1
    BOUNDED = "bounded"  # one behaviour change, <=3 files, verify cmd already fails right -> T1
    UNBOUNDED = "unbounded"  # can't name the verify command yet -> frontier from the start
    JUDGMENT = "judgment"  # no verify command can exist even in principle -> NEEDS_HUMAN


@dataclass
class Task:
    id: str
    intent: str  # one sentence
    verify_cmd: str  # the command that proves it -- usually .fleet/verify.sh
    allow_files: list[str]
    non_goals: list[str] = field(default_factory=list)
    klass: Klass = Klass.BOUNDED

    # runtime state the dispatcher reads
    attempts: int = 0
    tried: frozenset[tuple[str, str | None]] = field(default_factory=frozenset)
    #   (tier value, model) pairs already attempted -- route() climbs the ladder by
    #   returning the next rung not in this set.
    tag: str | None = None  # "hard" routes to Opus
    shape: str | None = None  # e.g. "bulk_mechanical"
    multimodal_input: bool = False  # video/audio/PDF -> T2b's only monopoly
    deterministic_fix_available: bool = False  # a rule/codemod exists -> T0
    prompt_tokens: int = 0  # harness-measured; gates T5 and the 8K local cap
    max_lines: int = 150  # scope-creep trigger ceiling