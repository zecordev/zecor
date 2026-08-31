# SPDX-License-Identifier: Apache-2.0
"""The escalation loop, end to end against a real temp git repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import FakeLedger, ScriptedWorker, make_repo
from zecor.cards import Card
from zecor.config import ESCALATIONS_PER_TASK
from zecor.orchestrator import run_task, task_fingerprint
from zecor.task import Klass
from zecor.tiers import Tier

# The repo's gate: green iff src.txt contains the token FIXED.
VERIFY = "grep -q FIXED src.txt && exit 0 || exit 1"


@pytest.fixture(autouse=True)
def _no_live_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """These are loop-control tests. The context-firewall brief and the post-land
    review each reach a live local model; stub both so the loop is tested against
    the scripted worker alone. Briefs are covered in test_brief/test_firewall, the
    review in test_review. One test overrides `review_branch` to assert a review crash
    still can't un-land a committed change."""
    import contextlib

    from zecor import orchestrator
    from zecor.review import ReviewResult

    monkeypatch.setattr(
        orchestrator, "review_branch", lambda *a, **k: ReviewResult(verdict="ship")
    )
    monkeypatch.setattr(
        orchestrator, "deep_review", lambda *a, **k: ReviewResult(verdict="ship")
    )
    monkeypatch.setattr(orchestrator, "_refresh_brief", lambda brief, vr, card: brief)
    # a real permit is fast here (empty registry) but avoid touching the fs at all
    monkeypatch.setattr(
        orchestrator, "acquire_permit", lambda *a, **k: contextlib.nullcontext()
    )


def card(tmp_path: Path, klass: Klass = Klass.BOUNDED) -> Card:
    return Card(
        id="0001",
        repo="demo",
        intent="make the gate pass",
        verify_cmd="./.fleet/verify.sh",
        allow_files=["src.txt"],
        klass=klass,
    )


def _write(token: str):
    def action(lane: Path) -> tuple[str, int]:
        (lane / "src.txt").write_text(f"// {token}\n")
        return ("pass", 0) if token == "FIXED" else ("fail", 1)

    return action


def _noop_fail(lane: Path) -> tuple[str, int]:
    return ("fail", 1)


def run(tmp_path: Path, actions: list, **kw: object):
    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    worker = ScriptedWorker(actions=actions)
    ledger = FakeLedger(**kw)  # type: ignore[arg-type]
    outcome = run_task(card(tmp_path), ledger, repo_root=repo, dispatcher=worker, now=_fake_clock())
    return outcome, worker, ledger


def _fake_clock():
    t = [0.0]

    def clock() -> float:
        t[0] += 1.0
        return t[0]

    return clock


def test_lands_on_the_first_local_attempt(tmp_path: Path) -> None:
    outcome, worker, ledger = run(tmp_path, [_write("FIXED")])
    assert outcome.state == "landed"
    assert worker.calls == [("t1", None)]
    assert ledger.attempts[-1]["outcome"] == "pass"


def test_two_local_attempts_then_escalates_and_t3_lands_it(tmp_path: Path) -> None:
    # T1 fails twice, T2a is dry so the ladder gives T3, which fixes it.
    outcome, worker, ledger = run(
        tmp_path,
        [_noop_fail, _noop_fail, _write("FIXED")],
        dry={Tier.T2A},
    )
    tiers = [c[0] for c in worker.calls]
    assert tiers == ["t1", "t1", "t3"]
    assert outcome.state == "landed"
    assert outcome.tiers_used == ["t1", "t3"]


def test_blocks_after_the_escalation_cap_and_records_a_dead_end(tmp_path: Path) -> None:
    outcome, worker, ledger = run(
        tmp_path,
        [_noop_fail],  # every attempt fails forever
        dry={Tier.T2A, Tier.T2B, Tier.T5},
    )
    assert outcome.state == "blocked"
    # 2 local + up to ESCALATIONS_PER_TASK ladder calls
    assert len(worker.calls) <= 2 + ESCALATIONS_PER_TASK + 1
    assert task_fingerprint(card(tmp_path)) in ledger.deadends


def test_recorded_dead_end_is_not_retried(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    worker = ScriptedWorker(actions=[_write("FIXED")])
    ledger = FakeLedger()
    ledger.deadends.add(task_fingerprint(card(tmp_path)))
    outcome = run_task(card(tmp_path), ledger, repo_root=repo, dispatcher=worker)
    assert outcome.state == "blocked"
    assert worker.calls == []  # never dispatched


def test_judgment_never_runs(tmp_path: Path) -> None:
    outcome, worker, _ = run(tmp_path, [_write("FIXED")])
    j = run_task(
        card(tmp_path, klass=Klass.JUDGMENT),
        FakeLedger(),
        repo_root=make_repo(tmp_path / "r2", verify_body=VERIFY),
        dispatcher=ScriptedWorker(actions=[_write("FIXED")]),
    )
    assert j.state == "needs-human-design"


def test_unrunnable_gate_is_flagged_not_attempted(tmp_path: Path) -> None:
    # verify command that cannot execute (127) -- a misconfigured card, not a bug fix.
    repo = make_repo(tmp_path / "repo", verify_body="exec this-command-does-not-exist")
    worker = ScriptedWorker(actions=[_write("FIXED")])
    outcome = run_task(card(tmp_path), FakeLedger(), repo_root=repo, dispatcher=worker)
    assert outcome.state == "gate-broken"
    assert worker.calls == []


def test_red_baseline_is_fine_a_bug_fix_starts_red(tmp_path: Path) -> None:
    # the failing gate IS the deliverable -- a red baseline must not abort
    outcome, worker, _ = run(tmp_path, [_write("FIXED")])
    assert outcome.state == "landed"


def test_green_baseline_lands_as_a_noop_without_dispatching(tmp_path: Path) -> None:
    # regression: a card whose gate is already green burned a real attempt trying to
    # "fix" a pass. It must land immediately, no worker call.
    repo = make_repo(tmp_path / "repo", verify_body="exit 0")
    worker = ScriptedWorker(actions=[_write("FIXED")])
    outcome = run_task(card(tmp_path), FakeLedger(), repo_root=repo, dispatcher=worker)
    assert outcome.state == "landed"
    assert "no change needed" in outcome.note
    assert worker.calls == []


def test_hard_card_skips_the_forced_local_warmup(tmp_path: Path) -> None:
    # regression: `local_owns_first` sent even a hard-tagged card to T1 first, wasting a
    # cycle before route() could hand it to Opus
    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    c = Card(
        id="0001",
        repo="demo",
        intent="make the gate pass",
        verify_cmd="./.fleet/verify.sh",
        allow_files=["src.txt"],
        klass=Klass.BOUNDED,
        tag="hard",
    )
    worker = ScriptedWorker(actions=[_write("FIXED")])
    outcome = run_task(c, FakeLedger(), repo_root=repo, dispatcher=worker, now=_fake_clock())
    assert outcome.state == "landed"
    assert [call[0] for call in worker.calls] == ["t4"]
    assert worker.calls[0][1] == "claude-opus-5"


def _rl(lane: Path) -> tuple[str, int]:
    return ("ratelimited", 1)


def test_vendor_unavailable_defers_it_never_blocks_or_poisons_the_cache(tmp_path: Path) -> None:
    # regression: refused/timeout/ratelimited (no real attempt) used to count as
    # escalations -> 4 dry vendors -> blocked + PERMANENT negative-cache entry.
    outcome, worker, ledger = run(tmp_path, [_rl])
    assert outcome.state == "deferred"
    assert task_fingerprint(card(tmp_path)) not in ledger.deadends
    assert len(worker.calls) <= 4  # NONRESULT_STREAK_CAP + a little, not the 5+ of before
    assert ledger.ratelimits  # the availability event was still recorded


def test_timeout_streak_defers_and_marks_tiers_dry(tmp_path: Path) -> None:
    def _to(lane: Path) -> tuple[str, int]:
        return ("timeout", 124)

    outcome, worker, ledger = run(tmp_path, [_to])
    assert outcome.state == "deferred"
    assert ledger.marked_dry  # each timed-out tier was recorded as dry (no RateLimit obj)


def test_first_escalation_resets_a_regressed_lane(tmp_path: Path) -> None:
    # regression: worst_failcount started at ~1e6 so the first escalation NEVER reset,
    # handing a scope-crept / broken worktree to the frontier tier.
    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)

    def out_of_scope(lane: Path) -> tuple[str, int]:
        (lane / "STRAY.txt").write_text("edited a file outside the allow-list\n")
        return ("fail", 1)

    def fix(lane: Path) -> tuple[str, int]:
        (lane / "src.txt").write_text("// FIXED\n")
        return ("pass", 0)

    worker = ScriptedWorker(actions=[out_of_scope, fix])
    ledger = FakeLedger(dry={Tier.T2A})  # ladder -> T3 for the fix
    outcome = run_task(card(tmp_path), ledger, repo_root=repo, dispatcher=worker, now=_fake_clock())
    assert outcome.state == "landed"
    # the stray file from the local attempt must have been reset before T3 ran
    assert "STRAY.txt" not in (outcome.lane_commit or "")
    import subprocess

    show = subprocess.run(
        ["git", "show", "--stat", "--format=", "zecor/0001"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "STRAY.txt" not in show
    assert "src.txt" in show


def test_review_failure_does_not_un_land_a_committed_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zecor import orchestrator
    from zecor.localmodel import LocalModelError

    def boom(*a: object, **k: object) -> object:
        raise LocalModelError("local model down during review")

    monkeypatch.setattr(orchestrator, "review_branch", boom)
    outcome, worker, _ = run(tmp_path, [_write("FIXED")])
    assert outcome.state == "landed"
    assert outcome.lane_commit
    assert "review skipped" in outcome.note


def test_over_cap_prompt_never_dispatches_to_the_local_tier(tmp_path: Path) -> None:
    # regression: route() saw task.prompt_tokens=0 (never populated), so its local-cap
    # guard was inert. The orchestrator now passes a real token count (exact via
    # zecor-tokcount when built), and a prompt over the 8K cap must skip T1 (it would
    # truncate) and climb the frontier. Varied text so a real BPE tokenizer can't merge
    # it down under the cap.
    big_non_goals = [" ".join(f"clause{i}word" for i in range(20_000))]  # >> 8K tokens
    c = Card(
        "0001",
        "demo",
        "make the gate pass",
        "./.fleet/verify.sh",
        ["src.txt"],
        non_goals=big_non_goals,
    )
    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    ledger = FakeLedger(dry={Tier.T3, Tier.T4})  # every frontier applier dry
    worker = ScriptedWorker(actions=[_noop_fail])
    outcome = run_task(c, ledger, repo_root=repo, dispatcher=worker, now=_fake_clock())
    assert outcome.state == "deferred"  # frontier dry, local skipped for the huge prompt
    assert "t1" not in outcome.tiers_used  # never truncated a local attempt


def test_narrow_gate_that_regresses_the_full_suite_is_not_landed(tmp_path: Path) -> None:
    # card runs a narrow gate; the repo's ./.fleet/verify.sh checks more and fails.
    repo = make_repo(
        tmp_path / "repo",
        verify_body='grep -q FIXED src.txt && ! grep -q BROKE src.txt',
    )
    c = Card("0001", "demo", "make it pass", "grep -q FIXED src.txt", ["src.txt"])

    def action(lane: Path) -> tuple[str, int]:
        (lane / "src.txt").write_text("// FIXED BROKE\n")  # narrow gate green, full gate red
        return ("pass", 0)

    worker = ScriptedWorker(actions=[action])
    outcome = run_task(c, FakeLedger(), repo_root=repo, dispatcher=worker, now=_fake_clock())
    assert outcome.state == "needs-human-design"
    assert "full suite regressed" in outcome.note


def test_needs_human_flip_after_a_commit_keeps_the_branch(tmp_path: Path) -> None:
    # regression (F1): self-review / the regression gate flips a committed land to
    # needs-human -- the verified commit and its branch MUST survive for re-triage.
    repo = make_repo(
        tmp_path / "repo",
        verify_body='grep -q FIXED src.txt && ! grep -q BROKE src.txt',
    )
    c = Card("0001", "demo", "x", "grep -q FIXED src.txt", ["src.txt"])

    def action(lane: Path) -> tuple[str, int]:
        (lane / "src.txt").write_text("// FIXED BROKE\n")
        return ("pass", 0)

    outcome = run_task(
        c, FakeLedger(), repo_root=repo,
        dispatcher=ScriptedWorker(actions=[action]), now=_fake_clock(),
    )
    assert outcome.state == "needs-human-design"
    assert outcome.lane_commit
    import subprocess

    branches = subprocess.run(
        ["git", "branch", "--list", "zecor/0001"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "zecor/0001" in branches  # not force-deleted


def test_task_wall_cap_defers_a_pathological_task(tmp_path: Path, monkeypatch) -> None:
    from zecor import orchestrator

    monkeypatch.setattr(orchestrator, "TASK_WALL_CAP_S", 5)  # tiny cap
    # a clock that jumps 10s per call -> the loop-top check trips on the 2nd iteration
    t = [0.0]

    def clock() -> float:
        t[0] += 10.0
        return t[0]

    outcome, worker, _ = run(tmp_path, [_noop_fail, _noop_fail, _noop_fail])
    # (run() uses its own _fake_clock; re-run directly with the jumpy clock)
    repo = make_repo(tmp_path / "r2", verify_body=VERIFY)
    outcome = run_task(
        card(tmp_path), FakeLedger(), repo_root=repo,
        dispatcher=ScriptedWorker(actions=[_noop_fail]), now=clock,
    )
    assert outcome.state == "deferred"
    assert "wall-clock cap" in outcome.note


def test_review_reopens_the_task_once_on_a_confirmed_high_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zecor import orchestrator
    from zecor.review import Finding, ReviewResult

    calls = {"n": 0}

    def fake_deep(*a: object, **k: object) -> ReviewResult:
        calls["n"] += 1
        if calls["n"] == 1:  # first review: a real defect to fix
            return ReviewResult(
                verdict="needs-work",
                findings=[Finding("adversarial", "high", "src.txt", 3, "off-by-one", "CONFIRMED")],
            )
        return ReviewResult(verdict="ship")  # after the reopen: clean

    monkeypatch.setattr(orchestrator, "deep_review", fake_deep)
    # multi-file so should_deep_review fires; two green attempts with DIFFERENT content
    c = Card("0001", "demo", "make the gate pass", "./.fleet/verify.sh", ["src.txt", "b.txt"])

    def fix1(lane: Path) -> tuple[str, int]:
        (lane / "src.txt").write_text("// FIXED\n")
        (lane / "b.txt").write_text("// first\n")
        return ("pass", 0)

    def fix2(lane: Path) -> tuple[str, int]:
        (lane / "b.txt").write_text("// second, addresses the finding\n")
        return ("pass", 0)

    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    outcome = run_task(
        c, FakeLedger(), repo_root=repo,
        dispatcher=ScriptedWorker(actions=[fix1, fix2]), now=_fake_clock(),
    )
    assert calls["n"] == 2  # reviewed, reopened, reviewed again
    assert outcome.state == "landed"


def test_reopen_repair_failure_goes_to_human_not_the_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zecor import orchestrator
    from zecor.review import Finding, ReviewResult

    monkeypatch.setattr(
        orchestrator, "deep_review",
        lambda *a, **k: ReviewResult(
            verdict="needs-work",
            findings=[Finding("adversarial", "high", "src.txt", 1, "bad", "CONFIRMED")],
        ),
    )
    c = Card("0001", "demo", "make the gate pass", "./.fleet/verify.sh", ["src.txt", "b.txt"])

    def land(lane: Path) -> tuple[str, int]:
        (lane / "src.txt").write_text("// FIXED\n")
        (lane / "b.txt").write_text("// a\n")
        return ("pass", 0)

    def go_red(lane: Path) -> tuple[str, int]:
        (lane / "b.txt").write_text("// worse\n")
        (lane / "src.txt").write_text("// broke it\n")  # gate now red
        return ("fail", 1)

    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    outcome = run_task(
        c, FakeLedger(), repo_root=repo,
        dispatcher=ScriptedWorker(actions=[land, go_red, land, land]), now=_fake_clock(),
    )
    assert outcome.state == "needs-human-design"
    assert "repair attempt failed" in outcome.note
    assert len(outcome.tiers_used) <= 2  # did NOT walk the whole ladder after the reopen
    assert outcome.lane_commit  # the first verified commit is kept


def test_reopen_repair_nonresult_stops_at_human_not_the_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zecor import orchestrator
    from zecor.review import Finding, ReviewResult

    monkeypatch.setattr(
        orchestrator, "deep_review",
        lambda *a, **k: ReviewResult(
            verdict="needs-work",
            findings=[Finding("adversarial", "high", "src.txt", 1, "bad", "CONFIRMED")],
        ),
    )
    c = Card("0001", "demo", "make the gate pass", "./.fleet/verify.sh", ["src.txt", "b.txt"])

    def land(lane: Path) -> tuple[str, int]:
        (lane / "src.txt").write_text("// FIXED\n")
        (lane / "b.txt").write_text("// a\n")
        return ("pass", 0)

    def timeout(lane: Path) -> tuple[str, int]:
        return ("timeout", 124)

    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    outcome = run_task(
        c, FakeLedger(), repo_root=repo,
        dispatcher=ScriptedWorker(actions=[land, timeout, land, land, land]), now=_fake_clock(),
    )
    assert outcome.state == "needs-human-design"
    assert "did not run" in outcome.note
    # exactly one repair dispatch after the land, then stop -- no ladder climb
    assert len(outcome.tiers_used) <= 2
    assert outcome.lane_commit


def test_fleet_retry_forces_the_first_attempt_tier(tmp_path: Path) -> None:
    c = Card("0001", "demo", "make the gate pass", "./.fleet/verify.sh", ["src.txt"])
    c.retry_tier = "t4"
    c.retry_model = "claude-opus-5"
    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    outcome = run_task(
        c, FakeLedger(), repo_root=repo,
        dispatcher=ScriptedWorker(actions=[_write("FIXED")]), now=_fake_clock(),
    )
    assert outcome.state == "landed"
    assert outcome.tiers_used == ["t4"]  # forced, skipped the local warm-up


def test_deterministic_fix_tries_t0_first_then_falls_to_t1(tmp_path: Path) -> None:
    c = Card("0001", "demo", "remove the stub", "./.fleet/verify.sh", ["src.txt"])
    c.deterministic_fix_available = True
    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    def _t0_refused(lane: Path) -> tuple[str, int]:
        return ("refused", -1)  # T0: no rule rewrote anything

    worker = ScriptedWorker(actions=[_t0_refused, _write("FIXED")])
    out = run_task(c, FakeLedger(), repo_root=repo, dispatcher=worker, now=_fake_clock())
    assert worker.calls[0] == ("t0", None)  # T0 first -- no forced local warm-up
    assert ("t1", None) in worker.calls  # T0 fell through to the local model
    assert out.state == "landed"


def test_racing_hook_adopts_the_winning_approach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZECOR_RACING", "1")
    from zecor import racing
    from zecor.workers.base import VerifyRun, WorkerResult

    monkeypatch.setattr(racing, "can_race", lambda advised: True)

    def disp(route, job):  # the refactor-first (T4) approach wins; minimal (T3) fails
        from pathlib import Path as _P
        if route.tier is Tier.T4:
            (_P(job.lane_dir) / "src.txt").write_text("// FIXED by the refactor approach\n")
            return WorkerResult(outcome="pass", verify=VerifyRun(0, "", ""))
        return WorkerResult(outcome="fail", verify=VerifyRun(1, "", ""))

    c = Card("0001", "demo", "a genuinely hard change", "./.fleet/verify.sh", ["src.txt"])
    c.tag = "hard"
    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    out = run_task(c, FakeLedger(), repo_root=repo, dispatcher=disp, now=_fake_clock())

    assert out.state == "landed"
    assert out.tiers_used == ["race"]
    assert "refactor-first won" in out.note
    assert out.lane_branch == "zecor/0001"  # winner renamed onto the canonical branch


def test_racing_hook_falls_through_when_no_approach_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZECOR_RACING", "1")
    from zecor import racing
    from zecor.workers.base import VerifyRun, WorkerResult

    monkeypatch.setattr(racing, "can_race", lambda advised: True)
    calls: list[str] = []

    def disp(route, job):
        calls.append(route.tier.value)
        # every racer fails; then the serial ladder's first real attempt fixes it
        from pathlib import Path as _P
        if len(calls) > 2:  # past the two race lanes
            (_P(job.lane_dir) / "src.txt").write_text("// FIXED serially\n")
            return WorkerResult(outcome="pass", verify=VerifyRun(0, "", ""))
        return WorkerResult(outcome="fail", verify=VerifyRun(1, "", ""))

    c = Card("0002", "demo", "hard but the race whiffs", "./.fleet/verify.sh", ["src.txt"])
    c.tag = "hard"
    repo = make_repo(tmp_path / "repo", verify_body=VERIFY)
    out = run_task(c, FakeLedger(), repo_root=repo, dispatcher=disp, now=_fake_clock())

    assert calls[:2] == ["t3", "t4"]  # the race ran first, both lanes
    assert len(calls) > 2  # then the serial ladder ran too
    assert out.state == "landed"  # and it landed