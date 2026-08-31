# SPDX-License-Identifier: Apache-2.0
"""Intake classification.

Class is decided at intake, not by discovering failure. If the local model is
unreachable, default to BOUNDED and flag -- a wrong label costs one wrong first hop,
not a silent budget leak.
"""

from __future__ import annotations

from dataclasses import dataclass

from zecor.config import MODEL_ROUTER
from zecor.localmodel import LocalModelError, complete
from zecor.task import Klass

_SYSTEM = (
    "You classify software tasks into exactly one class. Reply with ONE word, lowercase.\n"
    "mechanical: a deterministic rule could almost express it (rename, extract, dead code, "
    "import hygiene, dependency bump, test scaffolding, a fix matching a known shape).\n"
    "bounded: one behaviour change, <=3 files, and you can state the failing assertion "
    "before any code is written.\n"
    "unbounded: new subsystem, cross-cutting refactor, 'why is this flaky' -- you cannot "
    "name the verify command yet.\n"
    "judgment: architecture, API shape, security posture, 'should we' -- no verify command "
    "can exist even in principle."
)

_VALID = {c.value for c in Klass}


@dataclass
class Classification:
    klass: Klass
    flagged: bool = False
    note: str = ""


def classify(repo: str, intent: str, *, model: str = MODEL_ROUTER) -> Classification:
    prompt = f"repo: {repo}\ntask: {intent}\n\nclass:"
    try:
        out = complete(prompt, model=model, system=_SYSTEM, max_tokens=8, temperature=0.0)
    except LocalModelError as e:
        return Classification(
            Klass.BOUNDED, flagged=True, note=f"local model down ({e}); defaulted"
        )
    word = out.text.strip().lower().split()[0] if out.text.strip() else ""
    word = word.strip(".,:;\"'`")
    if word in _VALID:
        return Classification(Klass(word))
    return Classification(
        Klass.BOUNDED, flagged=True, note=f"unparseable label {out.text!r}; defaulted"
    )