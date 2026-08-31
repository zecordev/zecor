# SPDX-License-Identifier: Apache-2.0
"""console.nlu: the local-model fallback (Command Center build step 8)."""

from __future__ import annotations

import pytest

from zecor import localmodel
from zecor.console import nlu
from zecor.console.grammar import Parsed


def _reply(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(
        nlu.localmodel, "complete",
        lambda *a, **k: localmodel.Completion(text=text, in_tokens=1, out_tokens=1),
    )


def test_a_clean_retry_intent_becomes_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, '{"intent": "retry", "id": "0042", "model": "opus"}')
    got = nlu.classify("have another go at forty-two but use the big model")
    assert got == Parsed("retry", {"id": "0042", "model": "opus"})


def test_id_is_stripped_to_digits(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, '{"intent": "show", "id": "card #7"}')
    assert nlu.classify("show me card 7") == Parsed("show", {"id": "7"})


def test_missing_required_slot_is_a_clarify(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, '{"intent": "retry"}')
    got = nlu.classify("retry it")
    assert isinstance(got, nlu.Clarify) and "retry" in got.options


def test_unknown_intent_is_a_clarify_with_suggestions(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, '{"intent": "unknown"}')
    got = nlu.classify("what's stuck right now")
    assert isinstance(got, nlu.Clarify)
    assert "blocked" in got.options


def test_invented_repo_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, '{"intent": "add_inbox", "repo": "made-up", "desc": "do a thing"}')
    got = nlu.classify("add a task to made-up", repos=["web-client", "platform-toolkit"])
    assert isinstance(got, nlu.Clarify)


def test_valid_repo_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, '{"intent": "add_inbox", "repo": "web-client", "desc": "tighten the cap"}')
    got = nlu.classify("new web-client task: tighten the cap", repos=["web-client"])
    assert got == Parsed("add_inbox", {"repo": "web-client", "desc": "tighten the cap"})


def test_bad_tier_is_dropped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, '{"intent": "retry", "id": "5", "tier": "t99"}')
    assert nlu.classify("retry 5 on tier 99") == Parsed("retry", {"id": "5"})


def test_model_offline_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> object:
        raise localmodel.LocalModelError("connection refused")

    monkeypatch.setattr(nlu.localmodel, "complete", boom)
    got = nlu.classify("do something clever")
    assert isinstance(got, nlu.Unavailable) and "refused" in got.reason


def test_non_json_reply_is_a_clarify(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, "sorry I don't know")
    assert isinstance(nlu.classify("???"), nlu.Clarify)