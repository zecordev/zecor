# SPDX-License-Identifier: Apache-2.0
"""Inbox parsing + task-card round-trip."""

from __future__ import annotations

from pathlib import Path

from zecor.cards import Card, parse_inbox
from zecor.task import Klass


def test_parse_inbox_takes_repo_colon_intent() -> None:
    text = (
        "# a comment\n"
        "\n"
        "- platform-toolkit: badge shows the old status after a change\n"
        "-   company-ops : tighten the k8s memory limit\n"
        "not a task line\n"
    )
    items = parse_inbox(text)
    assert [i.repo for i in items] == ["platform-toolkit", "company-ops"]
    assert items[0].intent == "badge shows the old status after a change"
    assert items[1].intent == "tighten the k8s memory limit"


def test_card_render_parse_round_trip(tmp_path: Path) -> None:
    card = Card(
        id="0007",
        repo="platform-toolkit",
        intent="resolve nested route params before the segment split",
        verify_cmd="pnpm vitest run src/router",
        allow_files=["src/router/resolve.ts", "src/router/resolve.test.ts"],
        non_goals=["do not touch the public Router type"],
        klass=Klass.BOUNDED,
        tag="hard",
    )
    path = card.write(tmp_path)
    back = Card.parse(path.read_text())
    assert back.id == "0007"
    assert back.repo == "platform-toolkit"
    assert back.intent.startswith("resolve nested route params")
    assert back.verify_cmd == "pnpm vitest run src/router"
    assert back.allow_files == card.allow_files
    assert back.non_goals == card.non_goals
    assert back.klass is Klass.BOUNDED
    assert back.tag == "hard"


def test_routing_fields_round_trip_and_reach_the_task() -> None:
    # regression: to_task() dropped shape/multimodal_input/deterministic_fix_available,
    # so route()'s T0, T2b and bulk-mechanical branches were dead on the live path.
    card = Card(
        id="9",
        repo="r",
        intent="x",
        verify_cmd="./.fleet/verify.sh",
        allow_files=["a.ts"],
        shape="bulk_mechanical",
        multimodal_input=True,
        deterministic_fix_available=True,
    )
    t = card.to_task()
    assert t.shape == "bulk_mechanical"
    assert t.multimodal_input is True
    assert t.deterministic_fix_available is True

    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as d:
        back = Card.parse(card.write(_P(d)).read_text())
    assert back.shape == "bulk_mechanical"
    assert back.multimodal_input is True
    assert back.deterministic_fix_available is True


def test_planner_detects_multimodal_and_bulk() -> None:
    from zecor.planner import _has_multimodal, _shape_of

    assert _has_multimodal("match this figma mockup exactly")
    assert _has_multimodal("bug repro is in the attached screenshot")
    assert not _has_multimodal("rename the resolve function")
    assert _shape_of("rename useFoo to useBar everywhere in the repo") == "bulk_mechanical"
    assert _shape_of("update every import of the old path") == "bulk_mechanical"
    assert _shape_of("fix the off-by-one in pagination") is None


def test_card_to_task_carries_the_fields() -> None:
    card = Card(
        id="1",
        repo="r",
        intent="x",
        verify_cmd="./.fleet/verify.sh",
        allow_files=["a.ts"],
        klass=Klass.MECHANICAL,
    )
    task = card.to_task()
    assert task.verify_cmd == "./.fleet/verify.sh"
    assert task.klass is Klass.MECHANICAL
    assert task.allow_files == ["a.ts"]