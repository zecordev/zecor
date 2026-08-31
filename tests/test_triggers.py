# SPDX-License-Identifier: Apache-2.0
"""Table tests for the nine escalation triggers -- one case per trigger."""

from __future__ import annotations

from zecor import triggers
from zecor.task import Task
from zecor.triggers import WALL_CLOCK_CAP_S, Attempt


def mk_task(**kw: object) -> Task:
    base: dict[str, object] = dict(
        id="t", intent="x", verify_cmd=".fleet/verify.sh", allow_files=["src/a.ts"]
    )
    base.update(kw)
    return Task(**base)  # type: ignore[arg-type]


def att(**kw: object) -> Attempt:
    base: dict[str, object] = dict(
        n=1,
        verify_exit=0,
        failing_assertions=frozenset(),
        diff_hash="h",
        touched_files=frozenset({"src/a.ts"}),
        lines_changed=10,
        wall_s=1.0,
        prompt_tokens=1000,
    )
    base.update(kw)
    return Attempt(**base)  # type: ignore[arg-type]


def test_gate_still_red() -> None:
    cur, prev = att(verify_exit=1), att(verify_exit=1)
    assert triggers.gate_still_red(cur, prev, mk_task())
    assert not triggers.gate_still_red(att(verify_exit=0), prev, mk_task())


def test_no_progress() -> None:
    fa = frozenset({"test_a", "test_b"})
    assert triggers.no_progress(
        att(verify_exit=1, failing_assertions=fa),
        att(verify_exit=1, failing_assertions=fa),
        mk_task(),
    )
    assert not triggers.no_progress(
        att(verify_exit=1, failing_assertions=fa),
        att(verify_exit=1, failing_assertions=frozenset({"test_a"})),
        mk_task(),
    )


def test_thrash() -> None:
    assert triggers.thrash(att(diff_hash="abc"), att(diff_hash="abc"), mk_task())
    assert not triggers.thrash(att(diff_hash="abc"), att(diff_hash="xyz"), mk_task())
    assert not triggers.thrash(att(diff_hash=""), att(diff_hash=""), mk_task())


def test_new_breakage() -> None:
    cur = att(green_before=frozenset({"was_green"}), failing_assertions=frozenset({"was_green"}))
    assert triggers.new_breakage(cur, None, mk_task())
    ok = att(green_before=frozenset({"was_green"}), failing_assertions=frozenset({"target"}))
    assert not triggers.new_breakage(ok, None, mk_task())


def test_scope_creep_file() -> None:
    cur = att(touched_files=frozenset({"src/a.ts", "src/secret.ts"}))
    assert triggers.scope_creep(cur, None, mk_task(allow_files=["src/a.ts"]))


def test_scope_creep_lines() -> None:
    assert triggers.scope_creep(att(lines_changed=300), None, mk_task(max_lines=150))
    assert not triggers.scope_creep(att(lines_changed=140), None, mk_task(max_lines=150))


def test_context_pressure() -> None:
    assert triggers.context_pressure(att(prompt_tokens=9000), None, mk_task())
    assert not triggers.context_pressure(att(prompt_tokens=7000), None, mk_task())


def test_loop_stall() -> None:
    assert triggers.loop_stall(att(wall_s=WALL_CLOCK_CAP_S + 1), None, mk_task())
    assert not triggers.loop_stall(att(wall_s=10.0), None, mk_task())


def test_retrieval_miss() -> None:
    assert triggers.retrieval_miss(att(unresolved_symbol="resolveRoute"), None, mk_task())
    assert not triggers.retrieval_miss(att(unresolved_symbol=None), None, mk_task())


def test_explicit_unknown_needs_a_decision_not_a_fact() -> None:
    decision = att(
        question_for_escalation="should the token rotate on every 401?", names_a_decision=True
    )
    assert triggers.explicit_unknown(decision, None, mk_task())
    fact = att(question_for_escalation="what does resolveRoute return?", names_a_decision=False)
    assert not triggers.explicit_unknown(fact, None, mk_task())


def test_evaluate_collects_all_fired() -> None:
    cur = att(verify_exit=1, wall_s=WALL_CLOCK_CAP_S + 1, lines_changed=999)
    prev = att(verify_exit=1)
    fired = triggers.evaluate(cur, prev, mk_task(max_lines=150))
    names = {f.split(":")[0] for f in fired}
    assert {"gate_still_red", "loop_stall", "scope_creep"} <= names