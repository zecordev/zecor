# SPDX-License-Identifier: Apache-2.0
"""Property tests for route(). Assert invariants over a synthetic ledger,
not hand-picked examples."""

from __future__ import annotations

import itertools

import pytest

from tests.fakes import FakeLedger
from zecor.route import LOCAL_PROMPT_CAP, route
from zecor.task import Klass, Task
from zecor.tiers import DISPATCHABLE, Tier

CLOUD_TIERS = [Tier.T2A, Tier.T2B, Tier.T3, Tier.T4, Tier.T5]


def mk(**kw: object) -> Task:
    base: dict[str, object] = dict(
        id="t1",
        intent="do the thing",
        verify_cmd=".fleet/verify.sh",
        allow_files=["src/a.ts"],
    )
    base.update(kw)
    return Task(**base)  # type: ignore[arg-type]


def _all_dry_subsets() -> list[set[Tier]]:
    out: list[set[Tier]] = []
    for r in range(len(CLOUD_TIERS) + 1):
        for combo in itertools.combinations(CLOUD_TIERS, r):
            out.append(set(combo))
    return out


@pytest.mark.parametrize("dry", _all_dry_subsets())
def test_never_returns_a_dry_tier(dry: set[Tier]) -> None:
    ledger = FakeLedger(dry=dry)
    for klass in Klass:
        for attempts in (0, 1, 2, 3):
            for tag in (None, "hard"):
                for shape in (None, "bulk_mechanical"):
                    r = route(
                        mk(klass=klass, attempts=attempts, tag=tag, shape=shape, prompt_tokens=100),
                        ledger,
                    )
                    if r.tier in DISPATCHABLE and r.tier not in (Tier.T0, Tier.T1):
                        assert ledger.has(r.tier), f"routed to dry {r.tier} (dry={dry})"


@pytest.mark.parametrize("dry", _all_dry_subsets())
def test_judgment_never_routes_to_a_model(dry: set[Tier]) -> None:
    r = route(mk(klass=Klass.JUDGMENT), FakeLedger(dry=dry))
    assert r.tier is Tier.NEEDS_HUMAN


@pytest.mark.parametrize("dry", _all_dry_subsets())
def test_unbounded_never_routes_to_t1(dry: set[Tier]) -> None:
    r = route(mk(klass=Klass.UNBOUNDED, attempts=0), FakeLedger(dry=dry))
    assert r.tier is not Tier.T1


def test_readers_are_never_on_the_coding_ladder() -> None:
    # T2a (Gemini HTTP) and T5 (Luna HTTP) return text and touch no lane.
    # route() must never send coding work to them, whatever is dry.
    for klass in Klass:
        for attempts in (0, 1, 2, 3):
            for tag in (None, "hard"):
                r = route(mk(klass=klass, attempts=attempts, tag=tag, prompt_tokens=100),
                          FakeLedger())
                assert r.tier not in (Tier.T2A, Tier.T5)


def test_every_applier_dry_defers() -> None:
    dry = {Tier.T2A, Tier.T2B, Tier.T3, Tier.T4, Tier.T5}
    r = route(mk(attempts=2, prompt_tokens=100), FakeLedger(dry=dry))
    assert r.tier is Tier.DEFER


def test_over_cap_prompt_skips_local() -> None:
    # A first attempt whose prompt already blows the 8K local context must not go to T1
    # (it would truncate) -- it starts one rung up.
    r = route(mk(klass=Klass.BOUNDED, attempts=0, prompt_tokens=LOCAL_PROMPT_CAP + 1),
              FakeLedger())
    assert r.tier is not Tier.T1
    assert r.tier is Tier.T3


def test_deterministic_fix_goes_to_t0_first_then_falls_through() -> None:
    # The planner only sets the flag when a confirmed active rule matches -> try T0.
    r = route(mk(klass=Klass.BOUNDED, deterministic_fix_available=True), FakeLedger())
    assert r.tier is Tier.T0
    # T0 already tried (refused/failed) -> normal ladder, local first on attempt 0.
    tried = frozenset({("t0", None)})
    r2 = route(
        mk(klass=Klass.BOUNDED, deterministic_fix_available=True, tried=tried), FakeLedger()
    )
    assert r2.tier is Tier.T1
    # judgment still beats the flag -- never dispatched.
    j = route(mk(klass=Klass.JUDGMENT, deterministic_fix_available=True), FakeLedger())
    assert j.tier is Tier.NEEDS_HUMAN


def test_multimodal_defers_when_t2b_is_dry() -> None:
    # T2b is the only tier that takes media -- if it is dry, DEFER, never route to a
    # tier the ledger says is out.
    r = route(mk(multimodal_input=True), FakeLedger(dry={Tier.T2B}))
    assert r.tier is Tier.DEFER


def test_hard_with_attempts_zero_still_goes_straight_to_opus() -> None:
    # regression: `attempts == 0` used to short-circuit to T1 before the hard branch,
    # forcing a wasted local warm-up on a task tagged for Opus.
    r = route(mk(attempts=0, tag="hard"), FakeLedger())
    assert r.tier is Tier.T4 and r.model == "claude-opus-5"


def test_multimodal_routes_to_t2b() -> None:
    r = route(mk(multimodal_input=True), FakeLedger())
    assert r.tier is Tier.T2B


def test_first_attempt_bounded_goes_local() -> None:
    assert route(mk(klass=Klass.BOUNDED, attempts=0), FakeLedger()).tier is Tier.T1


def test_hard_tag_reaches_opus_not_sonnet() -> None:
    # regression: the hard->Opus branch used to sit AFTER the ladder and was unreachable,
    # so `hard` silently got Sonnet. It must now beat the ladder.
    r = route(mk(attempts=2, tag="hard"), FakeLedger())  # everything available
    assert r.tier is Tier.T4
    assert r.model == "claude-opus-5", "hard tag must route to Opus, not Sonnet"

    r2 = route(mk(attempts=2, tag=None), FakeLedger(dry={Tier.T3}))
    assert r2.model == "claude-sonnet-5", "un-tagged escalation ladders T3 -> Sonnet at T4"

    # T4 pool capped -> Terra fallback (a separate vendor), never idle on a hard task
    r3 = route(mk(attempts=2, tag="hard", prompt_tokens=100), FakeLedger(dry={Tier.T4}))
    assert r3.tier is Tier.T3 and r3.model == "gpt-5.6-terra"
    # T3 and the whole T4 pool dry -> DEFER, never a reader tier, never Opus
    r4 = route(mk(attempts=2, tag="hard", prompt_tokens=100), FakeLedger(dry={Tier.T3, Tier.T4}))
    assert r4.tier is Tier.DEFER


def test_every_route_carries_a_reason() -> None:
    for klass in Klass:
        r = route(mk(klass=klass, attempts=1), FakeLedger())
        assert r.reason, f"{klass} produced a route with no reason"