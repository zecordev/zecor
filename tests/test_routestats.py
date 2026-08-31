# SPDX-License-Identifier: Apache-2.0
"""routestats + EV-ordered routing (build-queue item G)."""

from __future__ import annotations

import pytest

from zecor import routestats
from zecor.route import route
from zecor.task import Klass, Task
from zecor.tiers import Tier


def test_land_rate_uses_a_beta_prior() -> None:
    assert routestats.TierStat(0, 0).land_rate() == 0.5  # no history -> neutral
    assert routestats.TierStat(10, 10).land_rate() > 0.9
    assert routestats.TierStat(10, 0).land_rate() < 0.1


def test_from_rows_aggregates_wins() -> None:
    rows = [
        ("t3", "gpt-5.6-terra", "pass"),
        ("t3", "gpt-5.6-terra", "fail"),
        ("t4", "claude-opus-5", "pass"),
    ]
    s = routestats.from_rows(rows)
    assert s[("t3", "gpt-5.6-terra")] == routestats.TierStat(2, 1)
    assert s[("t4", "claude-opus-5")] == routestats.TierStat(1, 1)


def test_order_by_ev_prefers_the_cheaper_higher_rate_rung() -> None:
    ladder = [("t3", "gpt-5.6-terra"), ("t4", "claude-sonnet-5"), ("t4", "claude-opus-5")]
    stats = {
        ("t3", "gpt-5.6-terra"): routestats.TierStat(20, 4),   # 0.23 rate, cost 1
        ("t4", "claude-sonnet-5"): routestats.TierStat(20, 18),  # 0.86 rate, cost 2
        ("t4", "claude-opus-5"): routestats.TierStat(20, 19),   # 0.90 rate, cost 4
    }
    order = routestats.order_by_ev(ladder, stats)
    # sonnet: 0.86 - .08*2 = .70 ; opus: 0.90 - .08*4 = .58 ; terra: .23 - .08 = .15
    assert order[0] == ("t4", "claude-sonnet-5")
    assert order[-1] == ("t3", "gpt-5.6-terra")


def test_order_by_ev_is_a_noop_with_no_history() -> None:
    ladder = [("t3", "gpt-5.6-terra"), ("t4", "claude-sonnet-5")]
    assert routestats.order_by_ev(ladder, {}) == ladder


class _Led:
    def __init__(self, stats: dict[tuple[str, str | None], tuple[int, int]]) -> None:
        self._stats = stats

    def has(self, tier: Tier) -> bool:
        return True

    def month_spend_usd(self) -> float:
        return 0.0

    def route_stats(self) -> dict[tuple[str, str | None], tuple[int, int]]:
        return self._stats


def _hard_task() -> Task:
    return Task(id="1", intent="x", verify_cmd="v", allow_files=[], klass=Klass.BOUNDED,
                tag="hard", attempts=1, tried=frozenset())


def test_ev_routing_off_keeps_the_fixed_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZECOR_EV_ROUTING", raising=False)
    led = _Led({("t4", "claude-sonnet-5"): (50, 49)})
    assert route(_hard_task(), led).model == "claude-opus-5"  # ladder order, unchanged


def test_ev_routing_on_reorders_by_learned_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_EV_ROUTING", "1")
    led = _Led({
        ("t4", "claude-opus-5"): (30, 3),    # opus tanking for us
        ("t4", "claude-sonnet-5"): (30, 27),  # sonnet strong
    })
    r = route(_hard_task(), led)
    assert r.tier is Tier.T4 and r.model == "claude-sonnet-5"


def test_ev_routing_on_but_no_stats_is_the_fixed_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_EV_ROUTING", "1")
    assert route(_hard_task(), _Led({})).model == "claude-opus-5"