# SPDX-License-Identifier: Apache-2.0
"""Layer 2 of the chatbot: a frontier model in the loop, off the control path.

Layers 0 and 1 are the control plane -- fast, local, never a network call between the
operator and STOP. Layer 2 is the opposite: it *is* a frontier call, used only for
read-mostly, judgement-adjacent asks ("why is 0016 stuck?", "draft three cards...").

Contract:

* `converse()` runs on a worker thread; it must never be on the UI thread.
* If the frontier is unreachable it returns `Unavailable`.
* Under a budget `throttle`/`pause` it falls back to the local reasoner.
* It may *propose* actions (a ```zecor fenced block); it never executes anything itself.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field

from zecor.console import grammar

_FENCE = re.compile(r"```(?:zecor)?\s*\n(.*?)```", re.S)

_SYSTEM = (
    "You are Zecor's console assistant -- a terse, dry, competent copilot for an "
    "autonomous software-build system. Answer the operator's question directly. No "
    "preamble, no cheerleading, no exclamation marks. If it helps to propose zecor "
    "actions, put each on its own line inside a ```zecor code block using this grammar: "
    "stop | go | run [until HH:MM] | plan | retry <id> [with <model>] [tier tN] | "
    "assign <id> <move-slug> | new <repo>: <what to do>. Never propose a command whose "
    "id or repo you are guessing. You do not execute anything; the operator confirms."
)


@dataclass(frozen=True)
class Reply:
    text: str
    proposed: list[grammar.Parsed] = field(default_factory=list)
    via: str = ""  # which tier answered (t4 / t3 / local)


@dataclass(frozen=True)
class Unavailable:
    reason: str


def _state_brief() -> str:
    """A compact snapshot of what Zecor is doing -- cards, roadmap, recent trouble."""
    lines: list[str] = []
    try:
        from zecor.core import view

        cards = view.cards()
        by_state: dict[str, int] = {}
        for c in cards:
            by_state[c.state] = by_state.get(c.state, 0) + 1
        tally = ", ".join(f"{k}={v}" for k, v in sorted(by_state.items())) or "none"
        lines.append(f"cards: {tally}")
        trouble = [c for c in cards if c.state in ("blocked", "needs-human-design", "gate-broken")]
        for c in trouble[:8]:
            last = view.attempts(c.id, limit=1)
            why = last[0].reason if last else "(no attempt yet)"
            lines.append(f"  {c.id} {c.repo} [{c.state}] {c.intent[:60]} -- {why[:80]}")
    except Exception as e:
        lines.append(f"(card read failed: {e})")
    with contextlib.suppress(Exception):  # the board summary is best-effort context
        from zecor.core import roadmap, view

        rolled = roadmap.rollup(roadmap.load(), view.cards())
        for m in rolled:
            for camp in m.children:
                lines.append(f"campaign {camp.slug}: {camp.status} {camp.landed}/{camp.total}")
    return "\n".join(lines)


def _history_brief(history: list[tuple[str, str]], *, keep: int = 8) -> str:
    return "\n".join(f"{who}: {text}" for who, text in history[-keep:])


def _extract_proposals(text: str) -> tuple[str, list[grammar.Parsed]]:
    proposals: list[grammar.Parsed] = []
    for block in _FENCE.findall(text):
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = grammar.parse(line)
            if isinstance(parsed, grammar.Parsed):
                proposals.append(parsed)
    clean = _FENCE.sub("", text).strip()
    return clean or text.strip(), proposals


def _ask_local(prompt: str, system: str) -> str | None:
    try:
        from zecor import localmodel
        from zecor.config import MODEL_REASONER

        out = localmodel.complete(
            prompt, model=MODEL_REASONER, system=system, max_tokens=600,
            temperature=0.2, timeout_s=90.0,
        )
        return out.text or None
    except Exception:
        return None


def converse(
    message: str,
    *,
    history: list[tuple[str, str]] | None = None,
    timeout_s: float = 45.0,
) -> Reply | Unavailable:
    msg = message.strip()
    if not msg:
        return Unavailable("empty message")

    prompt = (
        f"ZECOR STATE\n{_state_brief()}\n\n"
        + (f"RECENT CONVERSATION\n{_history_brief(history or [])}\n\n" if history else "")
        + f"OPERATOR\n{msg}\n"
    )

    throttled = False
    with contextlib.suppress(Exception):
        from zecor.core import budget

        throttled = budget.ceiling_state().state != "ok"

    if throttled:
        text = _ask_local(prompt, _SYSTEM)
        if text is None:
            return Unavailable("budget throttle and the local reasoner is down")
        clean, proposals = _extract_proposals(text)
        return Reply(clean, proposals, via="local (throttled)")

    try:
        from zecor.frontier import NoFrontierAvailable, ask
    except Exception as e:  # pragma: no cover - import guard
        return Unavailable(f"frontier module unavailable: {e}")

    try:
        text, tier = ask(f"{_SYSTEM}\n\n{prompt}", timeout_s=timeout_s)
    except NoFrontierAvailable:
        local = _ask_local(prompt, _SYSTEM)
        if local is None:
            return Unavailable("Layer-2 offline (no frontier, no local reasoner)")
        clean, proposals = _extract_proposals(local)
        return Reply(clean, proposals, via="local (frontier down)")
    except Exception as e:
        return Unavailable(f"Layer-2 error: {str(e)[:120]}")

    clean, proposals = _extract_proposals(text)
    return Reply(clean or "(no answer)", proposals, via=tier)