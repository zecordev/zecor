# SPDX-License-Identifier: Apache-2.0
"""console.converse: Layer 2 -- a frontier model in the loop, off the control path."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import config
from zecor.console import converse


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ZECOR_ROOT", tmp_path)
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db" / "zecor.db")
    (tmp_path / "tasks").mkdir()
    # budget: force "ok" so the frontier path is taken
    monkeypatch.setattr(
        "zecor.core.budget.ceiling_state", lambda **k: type("S", (), {"state": "ok"})()
    )


def _ok(text: str, tier: str = "t4"):
    return lambda prompt, timeout_s=45.0: (text, tier)


def test_plain_answer_no_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("zecor.frontier.ask", _ok("0016 is blocked on a missing symbol."))
    r = converse.converse("why is 0016 stuck?")
    assert isinstance(r, converse.Reply)
    assert "missing symbol" in r.text and r.proposed == [] and r.via == "t4"


def test_fenced_block_becomes_confirmable_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = (
        "You could retry it on a stronger model and file it under the contract move.\n"
        "```zecor\nretry 0016 with opus\nassign 0016 api-contract\n```\n"
    )
    monkeypatch.setattr("zecor.frontier.ask", _ok(answer, "t3"))
    r = converse.converse("what should I do about 0016?")
    assert isinstance(r, converse.Reply)
    verbs = [p.verb for p in r.proposed]
    assert verbs == ["retry", "assign"]
    assert "```" not in r.text  # the fence is stripped from the prose


def test_no_frontier_falls_back_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    from zecor.frontier import NoFrontierAvailable

    def _boom(p: str, timeout_s: float = 45.0) -> tuple[str, str]:
        raise NoFrontierAvailable("nothing")

    monkeypatch.setattr("zecor.frontier.ask", _boom)
    monkeypatch.setattr(converse, "_ask_local", lambda prompt, system: "local says: try t3")
    r = converse.converse("hi")
    assert isinstance(r, converse.Reply) and "local says" in r.text and "local" in r.via


def test_no_frontier_and_no_local_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from zecor.frontier import NoFrontierAvailable

    def _boom(p: str, timeout_s: float = 45.0) -> tuple[str, str]:
        raise NoFrontierAvailable("nothing")

    monkeypatch.setattr("zecor.frontier.ask", _boom)
    monkeypatch.setattr(converse, "_ask_local", lambda prompt, system: None)
    r = converse.converse("hi")
    assert isinstance(r, converse.Unavailable) and "offline" in r.reason


def test_throttle_uses_local_not_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "zecor.core.budget.ceiling_state", lambda **k: type("S", (), {"state": "throttle"})()
    )
    called = {"frontier": False}

    def _frontier(p: str, timeout_s: float = 45.0) -> tuple[str, str]:
        called["frontier"] = True
        return ("nope", "t4")

    monkeypatch.setattr("zecor.frontier.ask", _frontier)
    monkeypatch.setattr(converse, "_ask_local", lambda prompt, system: "throttled answer")
    r = converse.converse("draft a card")
    assert isinstance(r, converse.Reply)
    assert not called["frontier"] and "throttled" in r.via


def test_empty_message() -> None:
    assert isinstance(converse.converse("   "), converse.Unavailable)