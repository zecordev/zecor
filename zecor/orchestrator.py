# SPDX-License-Identifier: Apache-2.0
"""The escalation loop: intake classifies, local owns the first attempts, triggers
(observed from outside the model) decide escalation, the brief rides each hop up the
ladder, and the verify gate's exit code is the sole definition of done."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from zecor import provenance, racing, triggers
from zecor.authoring import author_failing_test, is_test_path, wants_test_first
from zecor.brief import Brief
from zecor.cards import Card
from zecor.config import ESCALATIONS_PER_TASK, LOCAL_ATTEMPTS, TASK_WALL_CAP_S
from zecor.failures import failing_assertions, passing_assertions
from zecor.firewall import brief_to_prompt, make_brief
from zecor.lanes import Lane, LaneError, close_lane, open_lane
from zecor.localmodel import LocalModelError
from zecor.permits import acquire as acquire_permit
from zecor.review import ReviewResult, _files_in_diff, review_branch
from zecor.route import LOCAL_PROMPT_CAP, Route, route
from zecor.rules import note_landed_fix
from zecor.selfreview import deep_review, should_deep_review
from zecor.task import Klass, Task
from zecor.tiers import Tier
from zecor.triggers import WALL_CLOCK_CAP_S, Attempt
from zecor.workers import Job, WorkerResult, dispatch
from zecor.workers.base import VerifyRun, run_capped, run_verify

Dispatcher = Callable[[Route, Job], WorkerResult]
Verifier = Callable[[Job], VerifyRun]


class LedgerLike(Protocol):
    """What run_task needs from the ledger."""

    def is_deadend(self, fingerprint: str) -> bool: ...
    def has(self, tier: Tier) -> bool: ...
    def month_spend_usd(self) -> float: ...
    def record_attempt(
        self,
        *,
        task_id: str,
        tier: Tier,
        model: str | None,
        in_tokens: int,
        out_tokens: int,
        verify_exit: int,
        outcome: str,
        reason: str,
    ) -> None: ...
    def observe_ratelimit(
        self, tier: Tier, *, window: str, used_now: int, resets_at: str
    ) -> None: ...
    def mark_dry(self, tier: Tier, *, window: str, hours: float) -> None: ...
    def note_deadend(self, fingerprint: str, approach: str, note: str) -> None: ...


FRONTIER = frozenset({Tier.T2A, Tier.T2B, Tier.T3, Tier.T4, Tier.T5})
NONRESULT = frozenset({"refused", "timeout", "ratelimited"})
NONRESULT_STREAK_CAP = 3  # this many "vendor unavailable" in a row -> DEFER, not blocked
LOCAL_PERMIT_WAIT_S = 300.0
MAX_REVIEW_REOPENS = 2
MIN_VIABLE_ATTEMPT_S = 60.0  # below this much task budget left, defer rather than dispatch
_REVIEW_DIFF_TOKEN_CAP = 60_000
_DRY_HOURS = {Tier.T2A: 24.0, Tier.T2B: 24 * 7.0, Tier.T3: 5.0, Tier.T4: 5.0, Tier.T5: 5.0}


@dataclass
class AttemptLog:
    n: int
    tier: str
    model: str | None
    outcome: str
    verify_exit: int
    reason: str
    triggers_fired: list[str] = field(default_factory=list)


@dataclass
class TaskOutcome:
    task_id: str
    state: str  # landed | blocked | deferred | needs-human-design | gate-broken
    attempts: list[AttemptLog] = field(default_factory=list)
    tiers_used: list[str] = field(default_factory=list)
    lane_branch: str | None = None
    lane_commit: str | None = None
    pr_url: str | None = None
    brief: Brief | None = None
    review: ReviewResult | None = None
    note: str = ""

    @property
    def frontier_in_tokens(self) -> int:
        return self._frontier[0]

    @property
    def frontier_out_tokens(self) -> int:
        return self._frontier[1]

    _frontier: tuple[int, int] = (0, 0)


def task_fingerprint(card: Card) -> str:
    key = f"{card.repo}\x00{card.intent}\x00{card.verify_cmd}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _early_exit(card: Card, ledger: LedgerLike, fp: str) -> tuple[str, str] | None:
    if ledger.is_deadend(fp):
        return "blocked", "recorded dead end -- not retried (negative cache)"
    if card.klass is Klass.JUDGMENT:
        return "needs-human-design", "judgment -- no verify command can exist even in principle"
    return None


@dataclass
class _Baseline:
    run: VerifyRun
    green_before: frozenset[str]
    done: bool = False  # `out` is already final -- skip the escalation loop
    commit_failed: bool = False  # a green commit raised -- keep the worktree


def _establish_baseline(
    card: Card,
    lane: Lane,
    out: TaskOutcome,
    *,
    dispatcher: Dispatcher,
    verifier: Verifier,
) -> _Baseline:
    """Run the gate once with no change; a green baseline means this is a feature ask,
    so author the failing test (or land as a no-op if the card doesn't want a test)."""
    run = verifier(_job(card, lane, None, ""))
    green_before = _green_set(run)
    bl = _Baseline(run, green_before)

    if run.exit in (126, 127):
        out.state = "gate-broken"
        out.note = f"verify command exits {run.exit} (not found / not executable)"
        bl.done = True
        return bl
    if not run.ok:
        return bl
    if not wants_test_first(card):
        out.state = "landed"
        out.note = "gate already green at baseline -- no change needed"
        out.lane_branch = None
        bl.done = True
        return bl

    auth = author_failing_test(card, lane, dispatcher=dispatcher, verifier=verifier)
    bl.done = True
    if not auth.ok:
        out.state = "needs-human-design"
        out.note = f"test-first: {auth.note}"
        return bl
    try:
        out.lane_commit = lane.commit(
            f"test: failing test for {card.intent}\n\nzecor task {card.id}",
            only=[f for f in lane.touched_files() if is_test_path(f)],
        )
    except LaneError as e:
        # must commit, or a later lane.reset() discards the test and the loop solves
        # against the original green suite
        out.state = "blocked"
        out.note = f"test-first: authored test would not commit ({e}) -- worktree kept"
        bl.commit_failed = True
        return bl
    if out.lane_commit is None:
        out.state = "needs-human-design"
        out.note = "test-first: the authored test produced no committable change"
        return bl
    bl.run = auth.verify or run  # now red -- seed the loop from it
    out.attempts.append(
        AttemptLog(0, Tier.T1.value, None, "pass", 0, "authored the failing test")
    )
    bl.done = False
    return bl


def _run_race(
    card: Card,
    lane: Lane,
    out: TaskOutcome,
    ledger: LedgerLike,
    *,
    repo_root: Path,
    dispatcher: Dispatcher,
    lock: AbstractContextManager[object],
    flock: AbstractContextManager[object],
    budget_s: float,
) -> tuple[Lane, bool]:
    """Opt-in (ZECOR_RACING=1): fan a hard / unbounded card into two lanes, keep the
    first to a green gate. A no-win race falls through to the serial loop on a fresh
    lane."""
    if not (racing.enabled() and (card.tag == "hard" or card.klass is Klass.UNBOUNDED)):
        return lane, False
    try:
        from zecor.resources import advise_lanes

        if not racing.can_race(advise_lanes()):
            return lane, False
        with lock:
            close_lane(lane, delete_branch=True)
        rr = racing.race(
            card, repo_root, dispatcher=dispatcher,
            base_ref=lane.base_sha or "HEAD", timeout_s=budget_s,
        )
        if rr.won:
            with lock:
                lane = racing.adopt(card.id, repo_root, rr)
            out.lane_branch = lane.branch
            out.lane_commit = rr.winner_commit
            out.tiers_used = ["race"]
            out.attempts.append(
                AttemptLog(1, "race", rr.winner_label, "pass", 0,
                           f"raced: {rr.winner_label} approach won")
            )
            out.state = "landed"
            out.note = f"raced {rr.lanes_run} approaches -- {rr.winner_label} won"
            _land_review(out, card, lane, ledger, flock)
            if out.state == "landed":
                _mine_and_pr(out, card, lane, ledger)
            return lane, True
        with lock:
            lane = open_lane(card.id, repo_root)
        out.lane_branch = lane.branch
        out.note = "race produced no green gate -- falling back to the ladder"
    except (LaneError, OSError) as e:
        out.note += f"  (racing skipped: {e})"
        if not (lane.path / ".git").exists():
            with lock:
                lane = open_lane(card.id, repo_root)
            out.lane_branch = lane.branch
    return lane, False


def _pick_route(
    card: Card,
    ledger: LedgerLike,
    *,
    brief: Brief | None,
    prior: list[str],
    attempts: int,
    tried: set[tuple[str, str | None]],
    local_owns_first: bool,
    local_tries: int,
) -> tuple[Route, Task, str]:
    """The prompt for this attempt and the route it takes: a `zecor retry --tier`
    override, then the forced local warm-up, then route()'s ladder."""
    prompt = _build_prompt(card, brief, prior)
    task = card.to_task()
    task.attempts = attempts
    task.tried = frozenset(tried)
    task.prompt_tokens = _estimate_tokens(prompt)
    forced = _forced_route(card, attempts, tried)
    if forced is not None:
        return forced, task, prompt
    if (
        attempts == 0
        and local_owns_first
        and local_tries < LOCAL_ATTEMPTS
        and task.prompt_tokens < LOCAL_PROMPT_CAP  # else the local context truncates
    ):
        r = Route(Tier.T1, reason=f"local attempt {local_tries + 1}/{LOCAL_ATTEMPTS}")
        return r, task, prompt
    return route(task, ledger), task, prompt


def _handle_verified(
    out: TaskOutcome,
    card: Card,
    lane: Lane,
    ledger: LedgerLike,
    flock: AbstractContextManager[object],
    *,
    attempt_no: int,
    reopens: int,
    remaining: float,
) -> tuple[str, bool, list[str]]:
    """The gate went green: commit, re-run the full suite for regressions, self-review.
    Returns (signal, commit_failed, findings) -- "break" means out is final, "reopen"
    means redispatch once with `findings` as a constraint."""
    msg = f"{card.intent}\n\nzecor task {card.id}; verified by `{card.verify_cmd}`"
    if provenance.wants_trailers(card.repo):
        with suppress(Exception):  # provenance must never sink a verified land
            msg = provenance.decorate(msg, provenance.gather(card, out), repo=card.repo)
    try:
        # commit everything that made the gate green (minus scratch); an `only=` scope
        # could leave an essential edit dirty for close_lane to discard
        out.lane_commit = lane.commit(msg)
    except LaneError as e:
        out.state = "blocked"
        out.note = f"gate green but commit failed: {e} -- worktree kept at {lane.path}"
        return "break", True, []
    if out.lane_commit is None:
        out.state = "needs-human-design" if reopens else "blocked"
        out.note = (
            "review reopen produced no change -- the model could not act on the finding; "
            "needs a human"
            if reopens
            else f"gate green after {attempt_no} attempt(s) but no in-scope change -- "
            "flaky gate or out-of-scope edit"
        )
        return "break", False, []
    regression = _full_suite_regressed(card, lane)
    if regression is not None:
        out.state = "needs-human-design"
        out.note = (
            f"narrow gate green after {attempt_no} attempt(s) but the repo's full suite "
            f"regressed -- {regression}"
        )
        return "break", False, []
    out.state = "landed"
    out.note = f"gate green after {attempt_no} attempt(s)"
    _land_review(out, card, lane, ledger, flock)
    if out.state != "landed":  # review's block verdict flipped it
        return "break", False, []
    fix_me = _reopenable_findings(out.review)
    if not fix_me:
        _mine_and_pr(out, card, lane, ledger)
        return "break", False, []
    if reopens < MAX_REVIEW_REOPENS and remaining > MIN_VIABLE_ATTEMPT_S:
        return "reopen", False, fix_me
    out.state = "needs-human-design"
    out.note += "  -- review finding unresolved, no PR: " + " | ".join(fix_me)
    return "break", False, []


def _finalize_lane(
    out: TaskOutcome,
    lane: Lane,
    lock: AbstractContextManager[object],
    *,
    commit_failed: bool,
) -> None:
    """Keep the branch when a human might open it (landed, blocked, or needs-human);
    commit_failed means the worktree is the only copy, so never remove it."""
    if commit_failed:
        return
    keep = out.lane_branch is not None and (
        out.state in ("landed", "blocked", "needs-human-design")
        or out.lane_commit is not None
    )
    try:
        with lock:
            close_lane(lane, delete_branch=not keep)
    except Exception as e:  # a git failure in cleanup must not sink a real outcome
        out.note += f"  (lane cleanup failed: {e})"


def run_task(
    card: Card,
    ledger: LedgerLike,
    *,
    repo_root: Path,
    dispatcher: Dispatcher = dispatch,
    verifier: Verifier = run_verify,
    now: Callable[[], float] = time.monotonic,
    git_lock: AbstractContextManager[object] | None = None,
    frontier_lock: AbstractContextManager[object] | None = None,
) -> TaskOutcome:
    out = TaskOutcome(task_id=card.id, state="blocked")
    lock = git_lock or nullcontext()
    flock = frontier_lock or nullcontext()
    fp = task_fingerprint(card)

    stop = _early_exit(card, ledger, fp)
    if stop is not None:
        out.state, out.note = stop
        return out

    with lock:
        lane = open_lane(card.id, repo_root)
    out.lane_branch = lane.branch
    in_tok = out_tok = 0
    commit_failed = False
    t_start = now()

    def _remaining() -> float:
        return t_start + TASK_WALL_CAP_S - now()

    def _wip(label: str) -> None:
        with suppress(LaneError):
            snap = lane.commit(f"WIP: {label}\n\nzecor task {card.id}")
            if snap:
                out.lane_commit = snap

    brief: Brief | None = None
    try:
        bl = _establish_baseline(card, lane, out, dispatcher=dispatcher, verifier=verifier)
        commit_failed = bl.commit_failed
        if bl.done:
            _finalize_lane(out, lane, lock, commit_failed=commit_failed)
            out._frontier = (in_tok, out_tok)
            return out
        baseline, green_before = bl.run, bl.green_before
        # "closer to green" is measured against the baseline failing count, not a sentinel
        best_failcount = len(failing_assertions(baseline.stdout, baseline.stderr, baseline.exit))

        lane, raced = _run_race(
            card, lane, out, ledger, repo_root=repo_root, dispatcher=dispatcher,
            lock=lock, flock=flock, budget_s=max(MIN_VIABLE_ATTEMPT_S, _remaining()),
        )
        if raced:
            _finalize_lane(out, lane, lock, commit_failed=commit_failed)
            out._frontier = (in_tok, out_tok)
            return out

        prior: list[str] = []
        prev_att: Attempt | None = None
        escalations = 0
        attempts = 0  # 0 while the local tier owns it; 1..N walks the ladder
        attempt_no = 0  # every dispatch, for the digest count
        local_tries = 0
        nonresult_streak = 0
        reopens = 0
        tried: set[tuple[str, str | None]] = set()  # (tier, model) route() must climb past
        tried_tiers: set[str] = set()  # tiers with a real attempt (blocked vs deferred)
        # a `hard` card or a deterministic-rule card skips the forced local warm-up
        local_owns_first = (
            card.klass in (Klass.MECHANICAL, Klass.BOUNDED)
            and card.tag != "hard"
            and not card.deterministic_fix_available
        )
        reopen_pending = False
        if card.retry_context:
            prior.append(f"human note on retry: {card.retry_context}")

        while True:
            if _remaining() <= 0:
                out.state = "deferred"
                out.note = f"task wall-clock cap ({TASK_WALL_CAP_S}s) hit -- requeue"
                _wip("wall-cap hit -- best attempt")
                break
            r, task, prompt = _pick_route(
                card, ledger, brief=brief, prior=prior, attempts=attempts, tried=tried,
                local_owns_first=local_owns_first, local_tries=local_tries,
            )
            if r.tier is Tier.NEEDS_HUMAN:
                out.state = "needs-human-design"
                out.note = r.reason
                break
            if r.tier is Tier.DEFER:
                # a rung merely dry is `deferred`; every applier rung tried and red is `blocked`
                ladder_tiers = {Tier.T2B} if card.multimodal_input else {Tier.T3, Tier.T4}
                a_rung_may_return = any(
                    t.value not in tried_tiers and not ledger.has(t) for t in ladder_tiers
                )
                if not tried_tiers or a_rung_may_return:
                    out.state = "deferred"
                    out.note = r.reason
                    _wip("deferred, ladder not ready -- best attempt")
                else:
                    out.state = "blocked"
                    out.note = f"every applier tier tried, still red -- {r.reason}"
                    _wip("blocked, ladder exhausted -- best attempt")
                    ledger.note_deadend(fp, "ladder-exhausted", out.note)
                break

            job = _job(card, lane, r.model, prompt)
            need_flock = r.tier in FRONTIER
            # a T1 attempt holds the model; a cross-process permit stops two lanes inferring
            # at once, bounded to what's left of the task budget -- no slot means DEFER
            permit_ctx: AbstractContextManager[object] = nullcontext()
            if r.tier is Tier.T1:
                got = acquire_permit(
                    "local_lane", timeout_s=max(1.0, min(LOCAL_PERMIT_WAIT_S, _remaining()))
                )
                if got is None:
                    out.state = "deferred"
                    out.note = "no local-model permit within the window -- machine busy"
                    _wip("deferred waiting on a local-model slot")
                    break
                permit_ctx = got
            # clamp after the permit/lock wait so the timeout reflects real remaining budget
            remaining = _remaining()
            if remaining < MIN_VIABLE_ATTEMPT_S:
                out.state = "deferred"
                out.note = f"only {remaining:.0f}s of the task budget left -- requeue"
                _wip("deferred, out of task budget")
                break
            job.timeout_s = min(job.timeout_s, remaining)
            with permit_ctx, (flock if need_flock else nullcontext()):
                # Another lane may have taken the last frontier slot while we waited.
                if need_flock and not ledger.has(r.tier):
                    if reopen_pending:
                        out.state = "needs-human-design"
                        out.note = "review-reopen repair could not dispatch -- last commit kept"
                        break
                    prior.append(f"[{r.tier.value}] slot taken by a concurrent lane")
                    nonresult_streak += 1
                    if nonresult_streak >= NONRESULT_STREAK_CAP:
                        out.state = "deferred"
                        out.note = "frontier slots kept getting taken -- requeue"
                        _wip("deferred, frontier slots contended -- best attempt")
                        break
                    attempts += 1
                    continue
                t0 = now()
                result = dispatcher(r, job)
                elapsed = now() - t0
                attempt_no += 1
                if r.tier.value not in out.tiers_used:
                    out.tiers_used.append(r.tier.value)
                if result.rate_limit is not None and r.tier in FRONTIER:
                    # observe_ratelimit keys the quota row by vendor; T0/T1 have none.
                    ledger.observe_ratelimit(
                        r.tier,
                        window=result.rate_limit.window,
                        used_now=result.rate_limit.used_now,
                        resets_at=result.rate_limit.resets_at,
                    )
                # Record the row inside the lock so the T4 window count can't race.
                rec_exit = result.verify.exit if result.verify else -1
                ledger.record_attempt(
                    task_id=card.id,
                    tier=r.tier,
                    model=r.model,
                    in_tokens=result.in_tokens,
                    out_tokens=result.out_tokens,
                    verify_exit=rec_exit,
                    outcome=result.outcome,
                    reason=r.reason,
                )
            in_tok += result.in_tokens
            out_tok += result.out_tokens
            out.attempts.append(
                AttemptLog(attempt_no, r.tier.value, r.model, result.outcome, -1, r.reason, [])
            )

            # no attempt actually happened (vendor down, local refused, or wall-clock cap) --
            # requeue rather than burn the frontier ladder while the local tier is wedged
            if result.outcome in NONRESULT:
                if result.rate_limit is None and r.tier in FRONTIER:
                    ledger.mark_dry(r.tier, window="rolling", hours=_DRY_HOURS.get(r.tier, 5.0))
                if reopen_pending:
                    # the one post-review repair attempt didn't run -- don't climb the ladder
                    out.state = "needs-human-design"
                    out.note = (
                        f"review-reopen repair attempt did not run ({result.outcome}) -- "
                        "last verified commit kept, needs a human"
                    )
                    break
                prior.append(f"[{r.tier.value}] {result.outcome} -- no attempt made")
                nonresult_streak += 1
                if nonresult_streak >= NONRESULT_STREAK_CAP:
                    out.state = "deferred"
                    out.note = "every tier unavailable this window -- requeue"
                    _wip("deferred, all tiers unavailable -- best attempt")
                    break
                if r.tier is Tier.T0:
                    # T0 "refused" is permanent for this task -- burn the rung, but a
                    # no-op codemod doesn't spend an attempt
                    tried.add((r.tier.value, r.model))
                    continue
                # a non-result on the FIRST local attempt keeps the warm-up alive rather
                # than burning it, else `attempts=1` permanently skips T1's retry
                if r.tier is Tier.T1 and attempts == 0 and (local_tries + 1) < LOCAL_ATTEMPTS:
                    local_tries += 1
                else:
                    attempts += 1
                continue
            nonresult_streak = 0
            tried.add((r.tier.value, r.model))  # a real attempt happened -- climb past it
            tried_tiers.add(r.tier.value)

            # fail fast if the applier left a file that won't parse -- skip the whole suite
            syn = _syntax_broken(lane)
            vr = (
                VerifyRun(1, "", syn)
                if syn
                else (result.verify or verifier(job))
            )
            fa = failing_assertions(vr.stdout, vr.stderr, vr.exit)
            att = _attempt_record(attempt_no, vr, lane, elapsed, prompt, green_before, brief)
            fired = triggers.evaluate(att, prev_att, task)
            out.attempts[-1].verify_exit = vr.exit
            out.attempts[-1].triggers_fired = fired

            if vr.ok:
                sig, cf, fix_me = _handle_verified(
                    out, card, lane, ledger, flock,
                    attempt_no=attempt_no, reopens=reopens, remaining=_remaining(),
                )
                commit_failed = commit_failed or cf
                if sig == "break":
                    break
                # sig == "reopen": redispatch once with the review finding as a constraint
                reopens += 1
                reopen_pending = True
                prior.append("review flagged, fix before it can land: " + " | ".join(fix_me))
                out.note = f"reopened after review (round {reopens})"
                attempts += 1
                prev_att = att
                continue

            prior.append(_summarize(att, r, result))

            if reopen_pending:
                # the post-review repair attempt went red -- don't feed it back into the
                # ladder; keep the last verified commit, hand it to a human
                out.state = "needs-human-design"
                out.note = (
                    f"review-reopen repair attempt failed ({vr.exit}) -- last verified "
                    "commit kept, needs a human"
                )
                break

            local_retry_left = (
                r.tier is Tier.T1
                and attempts == 0
                and (local_tries + 1) < LOCAL_ATTEMPTS
                and not fired
            )
            if local_retry_left:
                local_tries += 1
                prev_att = att
                best_failcount = min(best_failcount, len(fa))
                continue

            # -- escalate (a real failed attempt) --
            escalations += 1
            if escalations > ESCALATIONS_PER_TASK:
                why = "; ".join(fired) or "no progress"
                out.state = "blocked"
                out.note = f"{escalations - 1} escalations, still red -- {why}"
                # snapshot the closest attempt -- close_lane force-removes the worktree
                with suppress(LaneError):
                    out.lane_commit = lane.commit(
                        f"WIP: blocked after {escalations - 1} escalations -- best attempt\n\n"
                        f"zecor task {card.id}; gate still red"
                    )
                ledger.note_deadend(fp, r.tier.value, out.note)
                break

            brief = _refresh_brief(brief, vr, card)
            if len(fa) < best_failcount:
                best_failcount = len(fa)  # keep the lane -- this attempt got closer
            else:
                lane.reset()  # discard: not closer to green than the best so far
            attempts += 1
            prev_att = att

        out.brief = brief
    except LocalModelError as e:
        if out.state != "landed":
            out.state = "blocked"
            out.note = f"local model unreachable mid-task: {e}"
    except LaneError as e:  # a git op mid-loop -- don't crash the run
        if out.state not in ("landed", "blocked"):
            out.state = "blocked"
            out.note = f"lane git error mid-task: {e}"
    finally:
        _finalize_lane(out, lane, lock, commit_failed=commit_failed)
    out._frontier = (in_tok, out_tok)
    return out


def _land_review(
    out: TaskOutcome,
    card: Card,
    lane: Lane,
    ledger: LedgerLike,
    flock: AbstractContextManager[object],
) -> None:
    """Review the committed diff (deep review when warranted) and mine it for a
    recurring fix shape. Sets out.review; a `block` verdict flips the outcome to
    needs-human but never un-lands the verified change -- the draft PR is opened
    separately (`_mine_and_pr`) once the review-reopen decision is made."""
    from zecor import tokens

    try:
        diff = lane.committed_diff()
        # a pathologically large diff would blow the review model's context -- trim it to
        # a budget by dropping whole hunks (file headers kept), a no-op below the cap.
        diff = tokens.diff_trim(diff, _REVIEW_DIFF_TOKEN_CAP)
        # review_branch's frontier pass calls `claude` (T4) first (frontier.ask). Gate
        # it on T4 specifically -- not `has(T3) or has(T4)` -- so a capped Pro pool is
        # not hit; hold frontier_lock and record the call so the window cap sees it.
        want_frontier = ledger.has(Tier.T4)
        deep = should_deep_review(card, out.tiers_used, len(_files_in_diff(diff)))

        def _record(used: str) -> None:
            if used in ("t3", "t4"):
                ledger.record_attempt(
                    task_id=card.id, tier=Tier(used), model=None,
                    in_tokens=0, out_tokens=0, verify_exit=0, outcome="pass",
                    reason="self-review frontier call",
                )

        with flock if want_frontier else nullcontext():
            if deep:
                out.review = deep_review(
                    card, diff, lane_path=lane.path, base_sha=lane.base_sha,
                    ledger_has_frontier=want_frontier,
                    record_frontier=_record if want_frontier else None,
                    frontier_ok=lambda: ledger.has(Tier.T4),
                )
            else:
                out.review = review_branch(card, diff, ledger_has_frontier=want_frontier)
                if want_frontier:
                    for used in out.review.tier_used.split("+"):
                        _record(used)
        out.note += f"  ({out.review.summary_line()})"
        if out.review.verdict == "block":
            # A tautological gate or a confirmed defect -- do not leave a rosy PR.
            # The commit is verified and stays; a human re-triages.
            out.state = "needs-human-design"
            out.note += "  -- SELF-REVIEW BLOCKED, needs a human before it ships"
    except Exception as e:  # review is advisory -- the gate already decided
        out.note += f"  (review skipped: {e})"


def _mine_and_pr(out: TaskOutcome, card: Card, lane: Lane, ledger: LedgerLike) -> None:
    """Post-decision (no reopen pending): mine the final landed diff for a recurring
    fix shape and open the draft PR. All best effort -- never un-lands."""
    try:
        db = getattr(ledger, "db", None)
        if db is not None and out.state == "landed":
            rule_note = note_landed_fix(db, card.intent, lane.committed_diff())
            if rule_note:
                out.note += f"  ({rule_note})"
    except (sqlite3.Error, OSError) as e:
        out.note += f"  (rule mining skipped: {e})"
    if out.state == "landed" and out.lane_commit:
        # provenance: a git note on refs/notes/provenance + a jsonl line. Best effort --
        # wrap the whole call so nothing here can flip a landed card to blocked.
        try:
            out.note += "  (" + provenance.record(
                card, out, lane.path, repo=card.repo, sha=out.lane_commit
            ) + ")"
        except Exception as e:
            out.note += f"  (provenance skipped: {e})"
    try:
        tiers = " -> ".join(out.tiers_used) or "T1"
        review_md = out.review.as_markdown() if out.review else "(review skipped)"
        pr_body = (
            f"Zecor task {card.id}. Verified by `{card.verify_cmd}`.\n"
            f"Tiers: {tiers}. Review before marking ready.\n\n{review_md}"
        )
        if _has_secret(out, lane, pr_body):
            return  # branch kept, no PR -- see the note set by _has_secret
        out.pr_url = lane.open_draft_pr(title=card.intent[:72], body=pr_body)
    except Exception as e:  # must not un-land a verified, committed change
        out.note += f"  (draft PR not opened: {e})"


def _has_secret(out: TaskOutcome, lane: Lane, pr_body: str) -> bool:
    """Scan the landed diff and the PR text for secrets before the draft PR is opened.
    A hit flips the outcome to needs-human -- the change stays committed on the branch
    (never un-landed, ), but no PR carries the leak."""
    from zecor import secretscan

    try:
        diff = lane.committed_diff()
    except Exception as e:
        # fail closed: if we cannot read the diff, we cannot clear it -- no PR.
        out.state = "needs-human-design"
        out.note += f"  (SECRET SCAN could not read the diff ({e}) -- no PR opened)"
        return True
    hits = secretscan.scan_diff(diff) + secretscan.scan_text(pr_body, file="<pr-body>")
    if not hits:
        return False
    where = ", ".join(f"{h.rule}@{h.file}:{h.line}" for h in hits[:5])
    out.state = "needs-human-design"
    out.note += f"  (SECRET SCAN blocked the PR -- {where}; branch kept, no PR opened)"
    return True


# -- helpers --------------------------------------------------------------------


_TIER_OF = {"t0": Tier.T0, "t1": Tier.T1, "t2a": Tier.T2A, "t2b": Tier.T2B,
            "t3": Tier.T3, "t4": Tier.T4, "t5": Tier.T5}


def _forced_route(card: Card, attempts: int, tried: set[tuple[str, str | None]]) -> Route | None:
    """`zecor retry --tier` forces the FIRST attempt only; after that (or if the tier
    is unknown / already tried) the normal ladder takes over."""
    if not card.retry_tier or attempts != 0 or tried:
        return None
    tier = _TIER_OF.get(card.retry_tier.strip().lower())
    if tier is None or tier in (Tier.NEEDS_HUMAN, Tier.DEFER):
        return None
    return Route(tier, card.retry_model, reason=f"human retry -> forced {card.retry_tier}")


def _reopenable_findings(review: ReviewResult | None) -> list[str]:
    """The CONFIRMED, high-severity findings a re-dispatch could actually fix -- not a
    tautological-gate verdict (re-coding won't help) and not a scope note."""
    if review is None:
        return []
    return [
        f"{f.file}:{f.line} {f.summary}"
        for f in review.findings
        if f.severity == "high"
        and f.verdict == "CONFIRMED"
        and f.lens not in ("gate-quality", "scope")
    ]


_CANON_GATES = (
    ".zecor/verify.sh", "./.zecor/verify.sh", ".fleet/verify.sh", "fleet/verify.sh",
)


def _is_canon_gate(verify_cmd: str) -> bool:
    """True if the card's gate IS the repo's canonical `verify.sh` (any common spelling)
    -- then the full-suite re-run would be redundant."""
    s = verify_cmd.strip()
    for pre in ("bash ", "sh ", "uv run ", "./"):
        if s.startswith(pre):
            s = s[len(pre) :]
    return s.strip().rstrip("/") in _CANON_GATES


def _full_suite_regressed(card: Card, lane: Lane) -> str | None:
    """If the card ran a narrow gate and the repo has a canonical `verify.sh`, run that
    full gate on the committed lane. Returns a one-line reason on failure, else None.
    The script provably exists here, so 126/127 means it can't run (missing pytest,
    not executable) -- a real problem, surfaced, not silently treated as absent."""
    if _is_canon_gate(card.verify_cmd):
        return None
    rel = next(
        (r for r in (".zecor/verify.sh", ".fleet/verify.sh") if (lane.path / r).exists()),
        None,
    )
    if rel is None:
        return None
    rc, _out, err, killed = run_capped(
        ["/bin/sh", "-c", f"./{rel}"], cwd=lane.path, timeout_s=float(WALL_CLOCK_CAP_S)
    )
    if killed:
        return "full suite timed out"
    if rc == 0:
        return None
    if rc in (126, 127):
        return f"the repo's `./{rel}` cannot execute (exit {rc}) -- fix the gate"
    tail = (err or _out).strip().splitlines()[-1:] or ["exit " + str(rc)]
    return f"`./{rel}` exit {rc}: {tail[0][:160]}"


def _syntax_broken(lane: Lane) -> str:
    """A one-line reason if any file the applier touched does not parse, else ''."""
    try:
        from zecor import astscan

        touched = [f for f in lane.touched_files() if astscan.is_source(f)]
        issues = astscan.syntax_errors(lane.path, touched)
    except Exception:
        return ""
    if not issues:
        return ""
    head = issues[0]
    more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
    return f"{head.file}:{head.row} does not parse -- {head.text}{more}"


def _estimate_tokens(text: str) -> int:
    """Exact BPE count via `zecor-tokcount` when the Rust helper is built; otherwise a
    conservative char/byte heuristic (over-estimates on multibyte -- the safe direction
    for a cap guard)."""
    from zecor import tokens

    return tokens.count(text)


def _job(card: Card, lane: Lane, model: str | None, prompt: str) -> Job:
    return Job(
        prompt=prompt,
        lane_dir=lane.path,
        model=model,
        timeout_s=float(WALL_CLOCK_CAP_S),
        verify_cmd=card.verify_cmd,
        repo=card.repo,
        intent=card.intent,
    )


def _build_prompt(card: Card, brief: Brief | None, prior: list[str]) -> str:
    parts = [
        f"Repository: {card.repo}",
        f"Task: {card.intent}",
        f"You may only touch: {', '.join(card.allow_files) or '(any file in scope)'}",
    ]
    if card.non_goals:
        parts.append("Do NOT: " + "; ".join(card.non_goals))
    parts.append(
        f"Done means: `{card.verify_cmd}` exits 0. Make the SMALLEST change that does "
        "that -- do not fix unrelated issues you notice, refactor, or reformat; a "
        "separate task will handle those. A minimal diff merges cleanly with other lanes."
    )
    if brief is not None:
        parts += ["", "--- brief from the local tier ---", brief_to_prompt(brief, card.intent)]
    if prior:
        parts += ["", "--- prior attempts (do not repeat) ---", *prior]
    return "\n".join(parts)


def _attempt_record(
    n: int,
    vr: VerifyRun,
    lane: Lane,
    elapsed: float,
    prompt: str,
    green_before: frozenset[str],
    brief: Brief | None,
) -> Attempt:
    return Attempt(
        n=n,
        verify_exit=vr.exit,
        failing_assertions=failing_assertions(vr.stdout, vr.stderr, vr.exit),
        diff_hash=lane.diff_hash(),
        touched_files=lane.touched_files(),
        lines_changed=lane.lines_changed(),
        wall_s=elapsed,
        prompt_tokens=len(prompt) // 4,
        green_before=green_before,
        question_for_escalation=brief.question_for_escalation if brief else None,
        names_a_decision=brief.names_a_decision() if brief else False,
    )


def _green_set(vr: VerifyRun) -> frozenset[str]:
    # Test ids the baseline run reported PASSED. `new_breakage` fires if a later attempt
    # makes one of these fail. Empty when the runner does not enumerate passes -- a safe
    # degradation (the trigger just cannot fire).
    return passing_assertions(vr.stdout, vr.stderr)


def _summarize(att: Attempt, r: Route, result: WorkerResult) -> str:
    tag = f"{r.tier.value}/{r.model or '-'}"
    if result.outcome != "fail":
        return f"[{tag}] {result.outcome}: {(result.stderr or result.stdout or '').strip()[:160]}"
    return f"[{tag}] gate exit {att.verify_exit}; failing: {sorted(att.failing_assertions)[:4]}"


def _refresh_brief(brief: Brief | None, vr: VerifyRun, card: Card) -> Brief | None:
    artifact = (vr.stdout + "\n" + vr.stderr).strip()
    if not artifact:
        return brief
    try:
        return make_brief(artifact, context=f"repo {card.repo}; task: {card.intent}")
    except Exception:
        return brief  # keep the previous brief rather than lose the hop