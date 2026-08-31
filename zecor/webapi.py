# SPDX-License-Identifier: Apache-2.0
"""The command-center HTTP API -- pure request handlers, no `http.server` here.

`serve.py` routes to these and JSON-encodes the result; keeping the logic here makes it
unit-testable without a socket, and routes through the same `core/` modules the TUI
uses so the PWA and console cannot drift.

Reads: `state`, `transcript`, `roadmap`, `metrics`. Writes: `say` (a chat line) and
`intent` (an allow-listed verb). Nothing here touches the socket, auth, or SSE.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from zecor import config
from zecor.console import grammar
from zecor.core import actionlog, intents, questions, roadmap, transcript, view


def _card(c: view.CardView) -> dict[str, Any]:
    return {
        "id": c.id, "repo": c.repo, "intent": c.intent, "state": c.state,
        "klass": c.klass, "zombie": c.zombie, "retry_count": c.retry_count,
        "move": c.move, "age_s": None, "mtime": c.mtime,
    }


def state() -> dict[str, Any]:
    """One bundle the PWA polls: cards worst-first, the status numbers, open questions,
    and the momentum line."""
    from zecor.console import model as cmodel

    cards = view.cards()
    ordered = sorted(cards, key=lambda c: (cmodel.state_rank(c.state), c.id))
    try:
        h = view.health()
        health = {
            "stopped": h.stopped, "disk_free_gb": h.disk_free_gb,
            "disk_floor_gb": h.disk_floor_gb, "advised_lanes": h.advised_lanes,
            "max_lanes": h.max_lanes, "counts": h.counts, "note": h.resource_note,
        }
    except Exception as e:  # a probe blowing up must not blank the API
        health = {"stopped": config.stop_requested(), "note": f"health probe failed: {e}"}
    try:
        m = _metrics_summary()
        momentum = cmodel.momentum(
            m["total_lands"], m["today_lands"], m["spark"]
        )
    except Exception:
        momentum = ""
    return {
        "stopped": config.stop_requested(),
        "cards": [_card(c) for c in ordered],
        "health": health,
        "questions": [asdict(q) for q in questions.open_questions()],
        "momentum": momentum,
        "latest_turn": transcript.latest_id(),
    }


def transcript_since(since: int, limit: int = 200) -> dict[str, Any]:
    turns = transcript.tail(limit) if since <= 0 else transcript.since(since, limit)
    return {"turns": [asdict(t) for t in turns], "latest": transcript.latest_id()}


def roadmap_tree() -> dict[str, Any]:
    try:
        rolled = roadmap.rollup(roadmap.load(), view.cards())
    except Exception as e:
        return {"nodes": [], "error": str(e)}

    def node(r: roadmap.Rolled) -> dict[str, Any]:
        return {
            "kind": r.kind, "slug": r.slug, "title": r.title, "status": r.status,
            "landed": r.landed, "total": r.total,
            "children": [node(c) for c in r.children],
            "cards": list(r.cards),
        }

    return {"nodes": [node(r) for r in rolled]}


def _metrics_summary() -> dict[str, Any]:
    from zecor.core import metrics

    s = metrics.summary()
    return {
        "empty": s.empty, "total_attempts": s.total_attempts,
        "total_lands": s.total_lands, "est_cost_usd": s.est_cost_usd,
        "median_cycle_s": s.median_cycle_s, "p90_cycle_s": s.p90_cycle_s,
        "by_tier": [asdict(t) | {"rate": t.rate} for t in s.by_tier],
        "daily_lands": s.daily_lands,
        "today_lands": s.daily_lands[-1] if s.daily_lands else 0,
        "spark": metrics.sparkline([float(x) for x in s.daily_lands]),
    }


def metrics_view() -> dict[str, Any]:
    return _metrics_summary()


# -- writes -----------------------------------------------------------------

_INTENTS = ("stop", "go", "run", "plan", "retry", "assign", "clear_zombie", "undo", "answer")


def say(text: str) -> dict[str, Any]:
    """A chat line. Layer-0 grammar first (a control verb runs, no model); anything else
    goes to Layer-2 `converse`. Mirrors the console's chat box exactly."""
    text = " ".join(text.split())
    if not text:
        return {"ok": False, "message": "empty"}
    transcript.append("you", "message", text)

    parsed = grammar.parse(text)
    # Only *command-shaped* input short-circuits the model: an actionable verb, and the
    # whole line reads as a command (<= 6 words, not a question). "How do I stop flaky
    # runs?" must reach Layer-2, not touch STOP.
    if (
        isinstance(parsed, grammar.Parsed)
        and parsed.verb in _INTENTS
        and len(text.split()) <= 6
        and not text.rstrip().endswith("?")
    ):
        res = _run_verb(parsed.verb, parsed.args)
        transcript.append("zecor", "command", res.get("message", parsed.verb))
        return {"ok": res.get("ok", True), "kind": "command", "message": res.get("message", "")}

    from zecor.console import converse

    turns = transcript.tail(12)
    history = [(t.role, t.text) for t in turns]
    reply = converse.converse(text, history=history)
    if isinstance(reply, converse.Unavailable):
        msg = f"Layer-2 offline: {reply.reason}"
        transcript.append("zecor", "notice", msg)
        return {"ok": False, "kind": "unavailable", "message": msg}
    transcript.append("zecor", "reply", reply.text, meta={"via": reply.via})
    return {
        "ok": True, "kind": "reply", "message": reply.text,
        "proposed": [{"verb": p.verb, "args": p.args} for p in reply.proposed],
    }


def intent(verb: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    if verb not in _INTENTS:
        return {"ok": False, "message": f"unknown verb {verb!r}"}
    return _run_verb(verb, args or {})


def _run_verb(verb: str, args: dict[str, Any]) -> dict[str, Any]:
    def s(key: str) -> str:
        v = args.get(key)
        return str(v) if v not in (None, "") else ""

    if verb == "stop":
        r = intents.stop()
    elif verb == "go":
        r = intents.go()
    elif verb == "plan":
        r = intents.plan()
    elif verb == "run":
        r = intents.run(
            max_n=int(s("max")) if s("max").isdigit() else None, until=s("until") or None
        )
    elif verb == "retry":
        r = intents.retry(s("id"), tier=s("tier") or None, model=s("model") or None,
                          note=s("note") or None)
    elif verb == "assign":
        r = intents.assign_move(s("id"), s("move"))
    elif verb == "clear_zombie":
        r = intents.clear_zombie(s("id"))
    elif verb == "undo":
        u = actionlog.undo_last()
        return {"ok": u.ok, "message": u.message}
    elif verb == "answer":
        qid = s("qid")
        a = (
            questions.answer(int(qid), s("text"))
            if qid.isdigit()
            else questions.answer_for_card(s("id"), s("text"))
        )
        return {"ok": a.ok, "message": a.message}
    else:  # pragma: no cover - _INTENTS gate above
        return {"ok": False, "message": f"unhandled verb {verb!r}"}
    return {"ok": r.ok, "message": r.message}