# SPDX-License-Identifier: Apache-2.0
"""route(task, ledger) -> Route: routing, quota arithmetic and escalation as a
structured decision with no judgment in it -- written attended, property-tested
against a synthetic ledger, never changed unattended.

`task.tried` carries the (tier, model) pairs already attempted; each escalation
returns the next untried rung the ledger says is up, not the first available one,
or a failing task re-hits the same tier forever. Only the lane appliers are on the
ladder -- T1 (local/aider), T2b (`agy`, multimodal), T3 (Codex), T4 (Claude); T2a
(Gemini HTTP) and T5 (Luna HTTP) return text and touch no lane, so routing coding
work to them bills tokens for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from zecor.task import Klass, Task
from zecor.tiers import Tier

LOCAL_PROMPT_CAP = 8000  # confirmed by bench-local: ttft doubles from 8K to 16K


@dataclass(frozen=True)
class Route:
    tier: Tier
    model: str | None = None  # Terra/Sol inside T3; Sonnet/Opus inside T4
    reason: str = ""  # written to the attempt row so the digest can be audited

    def _key(self) -> tuple[str, str | None]:
        return (self.tier.value, self.model)


class Ledger(Protocol):
    def has(self, tier: Tier) -> bool: ...
    def month_spend_usd(self) -> float: ...


def _first_untried_up(ledger: Ledger, tried: frozenset[tuple[str, str | None]],
                      choices: list[Route]) -> Route | None:
    """The next rung not already attempted on this task that the ledger says is up.
    With `ZECOR_EV_ROUTING=1` the frontier rungs are reordered by learned expected value
    first -- same rungs, better order; a no-op with no history."""
    ordered = _ev_order(ledger, choices)
    for r in ordered:
        if r._key() in tried:
            continue
        if ledger.has(r.tier):
            return r
    return None


def _ev_order(ledger: Ledger, choices: list[Route]) -> list[Route]:
    from zecor import routestats

    if not routestats.enabled():
        return choices
    fn = getattr(ledger, "route_stats", None)
    stats_raw: dict[tuple[str, str | None], tuple[int, int]] = fn() if callable(fn) else {}
    stats = {k: routestats.TierStat(*v) for k, v in stats_raw.items()}
    if not stats:
        return choices
    ranked = routestats.order_by_ev([c._key() for c in choices], stats)
    by_key = {c._key(): c for c in choices}
    return [by_key[k] for k in ranked]


def route(task: Task, ledger: Ledger) -> Route:
    tried = task.tried

    # judgment must NEVER be dispatched, even if a deterministic fix looks available --
    # a stale/malformed card must not slip past via T0
    if task.klass is Klass.JUDGMENT:
        return Route(Tier.NEEDS_HUMAN, reason="judgment -- no verify command can exist")

    if task.deterministic_fix_available and ("t0", None) not in tried:
        return Route(Tier.T0, reason="a confirmed rule matches -- deterministic fix")

    if task.multimodal_input:
        # T2b (`agy`) is the only applier that accepts media; model is left to agy's own
        # config since the CLI's slugs drift and a stale hardcode is a false audit trail
        if ("t2b", None) not in tried and ledger.has(Tier.T2B):
            return Route(Tier.T2B, None, "multimodal input -- T2b's only monopoly")
        return Route(Tier.DEFER, reason="multimodal, T2b dry/tried -- nothing else takes media")

    # -- the applier ladder, in escalation order (climbed via `tried`) --------------
    if task.tag == "hard":
        # Terra is a separate vendor pool from Opus/Sonnet's shared T4 pool, so Sonnet
        # is only reachable if the window cap hasn't already bitten
        ladder = [
            Route(Tier.T4, "claude-opus-5", "tag=hard -> Opus"),
            Route(Tier.T3, "gpt-5.6-terra", "tag=hard, Opus tried/dry -- Terra"),
            Route(Tier.T4, "claude-sonnet-5", "tag=hard, Opus+Terra out -- Sonnet high-effort"),
        ]
        picked = _first_untried_up(ledger, tried, ladder)
        return picked or Route(
            Tier.DEFER, reason="tag=hard but every frontier applier dry -- requeue"
        )

    if task.klass is Klass.UNBOUNDED:
        ladder = [
            Route(Tier.T3, "gpt-5.6-terra", "unbounded -- skip T1 entirely"),
            Route(Tier.T4, "claude-sonnet-5", "unbounded -- ladder"),
            Route(Tier.T4, "claude-opus-5", "unbounded -- ladder top"),
        ]
        picked = _first_untried_up(ledger, tried, ladder)
        return picked or Route(Tier.DEFER, reason="unbounded, every frontier applier dry")

    # bounded / mechanical: local owns the first attempt (the orchestrator forces two
    # T1 tries before it ever calls route()); skip local only if the prompt already
    # blows its context cap -- a truncated local attempt is worse than starting a rung up
    if (
        task.attempts == 0
        and ("t1", None) not in tried
        and task.prompt_tokens < LOCAL_PROMPT_CAP
    ):
        return Route(Tier.T1, reason="first attempt -- local owns bounded/mechanical")

    ladder = [
        Route(Tier.T3, "gpt-5.6-terra", "escalation ladder"),
        Route(Tier.T4, "claude-sonnet-5", "escalation ladder"),
        Route(Tier.T4, "claude-opus-5", "escalation ladder -- top"),
    ]
    picked = _first_untried_up(ledger, tried, ladder)
    return picked or Route(Tier.DEFER, reason="every applier tier tried or dry -- requeue")