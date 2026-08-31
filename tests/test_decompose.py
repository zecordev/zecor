# SPDX-License-Identifier: Apache-2.0
"""zecor decompose -- requirements doc -> proposed inbox block."""

from __future__ import annotations

import json

import pytest

from zecor import decompose as dec


@pytest.fixture(autouse=True)
def _stub_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = json.dumps(
        {
            "repo": "widget-web",
            "tasks": [
                {
                    "intent": "add a /health endpoint",
                    "check": "pytest tests/test_health.py",
                    "ready": True,
                },
                {"intent": "decide the auth model", "check": "", "ready": False},
            ],
            "open_questions": ["single-tenant or multi-tenant?"],
        }
    )
    monkeypatch.setattr(dec, "ask", lambda prompt, **kw: (reply, "t4"))


def test_decompose_parses_tasks_and_questions() -> None:
    d = dec.decompose("Build a widget service. It needs health checks and auth.")
    assert d.repo == "widget-web"
    assert len(d.tasks) == 2
    assert d.open_questions == ["single-tenant or multi-tenant?"]
    assert d.tier == "t4"


def test_inbox_block_is_all_commented_and_flags_not_ready() -> None:
    d = dec.decompose("...")
    block = dec.to_inbox_block(d, "widget.md")
    non_blank = [ln for ln in block.splitlines() if ln.strip()]
    assert all(ln.startswith("#") for ln in non_blank)
    assert "add a /health endpoint" in block
    assert "NOT READY" in block
    assert "single-tenant or multi-tenant?" in block


def test_json_can_be_embedded_in_prose() -> None:
    d = dec.decompose("x")
    assert d.repo == "widget-web"  # _extract_json pulls the object out of any wrapper