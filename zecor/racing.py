# SPDX-License-Identifier: Apache-2.0
"""Parallel approach racing -- build-queue item H.

For a hard card, two different approaches to the same task run in separate lanes at the
same time; the first whose gate goes green wins and the others are thrown away -- a
frontier call is cheap next to a night lost to the wrong approach. `race()` is the
engine, plus a `zecor race <id>` command to drive it by hand; not yet threaded into
`run_task`'s hot path. Gated behind `ZECOR_RACING=1` and 2 lanes max, since a race
needs two frontier slots and two worktrees (`can_race()` defers to the resource
governor).
"""

from __future__ import annotations

import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from zecor.cards import Card
from zecor.lanes import Lane, _git, close_lane, open_lane
from zecor.route import Route
from zecor.tiers import Tier
from zecor.workers import dispatch as default_dispatch
from zecor.workers.base import Job, WorkerResult

MAX_LANES = 2


@dataclass(frozen=True)
class Approach:
    label: str
    route: Route
    preamble: str


def default_approaches(_card: Card) -> list[Approach]:
    return [
        Approach(
            "minimal",
            Route(Tier.T3, "gpt-5.6-terra", "race: minimal-diff approach"),
            "Approach: the SMALLEST possible change. Touch as few lines as you can.",
        ),
        Approach(
            "refactor-first",
            Route(Tier.T4, "claude-sonnet-5", "race: refactor-then-fix approach"),
            "Approach: if the code fights the change, refactor the immediate area FIRST, "
            "then make the fix cleanly.",
        ),
    ]


def enabled() -> bool:
    return os.environ.get("ZECOR_RACING", "").strip() not in ("", "0", "false", "no")


def can_race(advised_lanes: int) -> bool:
    """Two lanes need two of everything -- only race when the governor says there's room."""
    return enabled() and advised_lanes >= MAX_LANES


@dataclass
class RaceResult:
    winner_branch: str | None = None
    winner_label: str | None = None
    winner_path: Path | None = None
    winner_commit: str | None = None
    base_sha: str = ""
    lanes_run: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def won(self) -> bool:
        return self.winner_branch is not None


def _run_one(app: Approach, lane: Lane, card: Card, dispatcher: object, timeout_s: float) -> bool:
    job = Job(
        prompt=f"{app.preamble}\n\nTask: {card.intent}",
        lane_dir=lane.path,
        model=app.route.model,
        timeout_s=timeout_s,
        verify_cmd=card.verify_cmd,
        repo=card.repo,
        intent=card.intent,
    )
    try:
        result: WorkerResult = dispatcher(app.route, job)  # type: ignore[operator]
    except Exception:  # a racer blowing up must not sink the whole race
        return False
    return result.outcome == "pass" and result.verify is not None and result.verify.ok


def race(
    card: Card,
    repo_dir: Path,
    *,
    approaches: list[Approach] | None = None,
    dispatcher: object = None,
    timeout_s: float = 900.0,
    base_ref: str = "HEAD",
) -> RaceResult:
    """Run each approach in its own lane; the first to a green gate wins, the rest are
    closed. Returns the winning branch (already committed) or an empty result."""
    apps = (approaches or default_approaches(card))[:MAX_LANES]
    disp = dispatcher or default_dispatch
    res = RaceResult(lanes_run=len(apps))
    lanes: list[Lane] = []
    try:
        for i, _ in enumerate(apps):
            lanes.append(open_lane(f"{card.id}-r{i}", repo_dir, base_ref))
    except Exception as e:  # could not even set up -- clean and bail
        for ln in lanes:
            close_lane(ln, delete_branch=True)
        res.notes.append(f"race setup failed: {e}")
        return res

    winner_idx: int | None = None
    with ThreadPoolExecutor(max_workers=len(apps)) as pool:
        futs = {
            pool.submit(_run_one, apps[i], lanes[i], card, disp, timeout_s): i
            for i in range(len(apps))
        }
        pending = set(futs)
        while pending and winner_idx is None:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                i = futs[f]
                if f.result():
                    winner_idx = i
                    break

    for i, ln in enumerate(lanes):
        if i == winner_idx:
            sha = ln.commit(f"{card.intent}\n\nzecor race ({apps[i].label}); task {card.id}")
            if sha:
                res.winner_branch = ln.branch
                res.winner_label = apps[i].label
                res.winner_path = ln.path
                res.winner_commit = sha
                res.base_sha = ln.base_sha
                res.notes.append(f"{apps[i].label} won -> {ln.branch}")
            else:  # green but empty diff -- treat as no winner
                close_lane(ln, delete_branch=True)
                res.notes.append(f"{apps[i].label} passed but changed nothing")
        else:
            close_lane(ln, delete_branch=True)
            res.notes.append(f"{apps[i].label} closed")
    if not res.won:
        res.notes.append("no approach reached a green gate")
    return res


def adopt(card_id: str, repo_dir: Path, rr: RaceResult) -> Lane:
    """Move the winning race branch onto the canonical `zecor/<id>` name and return a
    Lane for it, so the normal review + land path can take over unchanged. The caller
    must already have closed the placeholder `zecor/<id>` lane."""
    if not (rr.won and rr.winner_path and rr.winner_branch):
        raise ValueError("adopt() needs a won RaceResult")
    canon = f"zecor/{card_id}"
    if rr.winner_branch != canon:
        _git(["branch", "-m", rr.winner_branch, canon], cwd=repo_dir)
    return Lane(card_id, repo_dir, rr.winner_path, canon, base_sha=rr.base_sha)