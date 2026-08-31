# SPDX-License-Identifier: Apache-2.0
"""zecor serve -- the command-center HTTP API + PWA shell."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import config, serve, webapi
from zecor.cards import Card, Klass
from zecor.core import transcript


@pytest.fixture(autouse=True)
def _tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "zecor"
    for name, sub in [
        ("ZECOR_ROOT", ""), ("TASKS_DIR", "tasks"), ("DIGEST_DIR", "digest"),
        ("LOGS_DIR", "logs"), ("DB_PATH", "db/zecor.db"), ("STOP_FILE", "STOP"),
        ("QUEUE_INBOX", "queue/inbox.md"),
    ]:
        monkeypatch.setattr(config, name, root / sub if sub else root)
    (root / "tasks").mkdir(parents=True)
    (root / "roadmap").mkdir()
    (root / "roadmap" / "m.md").write_text(
        "# Mission: M {slug: m}\n.\n\n## Campaign: C {slug: c}\n.\n\n"
        "### Move: One {slug: mv-one}\nDone when one.\n"
    )
    return root


def _card(cid: str, state: str, **kw: object) -> None:
    c = Card(cid, "demo", f"do {cid}", "./v.sh", ["a.py"], klass=Klass.BOUNDED, **kw)  # type: ignore[arg-type]
    c.state = state
    (config.TASKS_DIR / f"{cid}.md").write_text(c.render())


# -- webapi (pure) --------------------------------------------------------------

def test_state_bundles_cards_status_and_questions() -> None:
    _card("0001", "ready")
    _card("0002", "blocked")
    st = webapi.state()
    assert {c["id"] for c in st["cards"]} == {"0001", "0002"}
    assert st["cards"][0]["state"] == "blocked"  # worst-first
    assert "stopped" in st and "health" in st and "momentum" in st
    from zecor.core import questions
    questions.scan([type("C", (), {"id": "0002", "state": "blocked"})()])
    assert webapi.state()["questions"][0]["card_id"] == "0002"


def test_say_runs_a_control_verb_without_a_model() -> None:
    r = webapi.say("stop")
    assert r["ok"] and r["kind"] == "command"
    assert config.stop_requested()
    kinds = [t.kind for t in transcript.tail()]
    assert "message" in kinds and "command" in kinds


def test_say_routes_a_question_to_layer2(monkeypatch: pytest.MonkeyPatch) -> None:
    from zecor.console import converse
    monkeypatch.setattr(
        converse, "converse",
        lambda text, **kw: converse.Reply("because the gate exits 127", via="t4"),
    )
    r = webapi.say("why is 0002 stuck?")
    assert r["kind"] == "reply" and "127" in r["message"]
    assert transcript.tail()[-1].kind == "reply"


def test_intent_allow_list() -> None:
    assert webapi.intent("stop")["ok"] is True
    assert webapi.intent("rm -rf")["ok"] is False
    assert "unknown verb" in webapi.intent("delete_everything")["message"]


def test_transcript_since() -> None:
    transcript.append("you", "message", "a")
    mark = transcript.latest_id()
    transcript.append("zecor", "reply", "b")
    out = webapi.transcript_since(mark)
    assert [t["text"] for t in out["turns"]] == ["b"]


def test_roadmap_tree_shape() -> None:
    out = webapi.roadmap_tree()
    assert out["nodes"] and out["nodes"][0]["kind"] == "mission"


# -- Handler auth -------------------------------------------------------------

def test_api_requires_the_token_but_the_shell_does_not() -> None:
    h = serve.Handler.__new__(serve.Handler)
    serve.Handler.token = "sekret"
    h.headers = {}  # type: ignore[assignment]
    try:
        h.path = "/api/state?t=sekret"
        assert h._authed() is True
        h.path = "/api/state?t=wrong"
        assert h._authed() is False
        h.path = "/api/state"
        assert h._authed() is False
        h.headers = {"Authorization": "Bearer sekret"}  # type: ignore[assignment]
        h.path = "/api/state"
        assert h._authed() is True
    finally:
        serve.Handler.token = None