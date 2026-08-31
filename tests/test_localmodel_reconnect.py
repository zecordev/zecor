# SPDX-License-Identifier: Apache-2.0
"""localmodel: a dropped LM Studio connection retries before giving up (RESILIENCE)."""

from __future__ import annotations

import urllib.error

import pytest

from zecor import localmodel


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(localmodel.time, "sleep", lambda _s: None)


def test_retries_a_dropped_connection_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"ok"}}],"usage":{}}'

    def flaky(_req: object, timeout: float = 0) -> _Resp:
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection reset")
        return _Resp()

    monkeypatch.setattr(localmodel.urllib.request, "urlopen", flaky)
    out = localmodel.complete("hi", timeout_s=1.0)
    assert out.text == "ok" and calls["n"] == 3


def test_gives_up_after_the_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    def always_fails(_req: object, timeout: float = 0) -> object:
        raise TimeoutError("nope")

    monkeypatch.setattr(localmodel.urllib.request, "urlopen", always_fails)
    with pytest.raises(localmodel.LocalModelError):
        localmodel.complete("hi", timeout_s=1.0)


def test_garbage_body_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class _Bad:
        def __enter__(self) -> _Bad:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return b"not json"

    def once(_req: object, timeout: float = 0) -> _Bad:
        calls["n"] += 1
        return _Bad()

    monkeypatch.setattr(localmodel.urllib.request, "urlopen", once)
    with pytest.raises(localmodel.LocalModelError):
        localmodel.complete("hi", timeout_s=1.0)
    assert calls["n"] == 1  # a 200 with a bad body is not a transient drop