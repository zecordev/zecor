# SPDX-License-Identifier: Apache-2.0
"""zecor.notify: reach the operator off-screen (transcript turn + optional push)."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import config, notify
from zecor.core import transcript


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db" / "zecor.db")
    for k in ("ZECOR_NTFY_TOPIC", "ZECOR_NTFY_URL", "ZECOR_PUSHOVER_TOKEN", "ZECOR_PUSHOVER_USER"):
        monkeypatch.delenv(k, raising=False)


def test_notify_always_records_a_transcript_turn() -> None:
    r = notify.notify("zombie 0016", "its lease outlived it", level="warn")
    assert r.transcript_id > 0 and r.channels == [] and r.errors == []
    (t,) = transcript.tail()
    assert t.kind == "notice" and t.role == "zecor"
    assert "zombie 0016" in t.text and t.meta["level"] == "warn"


def test_ntfy_fires_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_NTFY_TOPIC", "zecor-test-abc")
    seen: dict[str, object] = {}

    def fake_post(req: object) -> None:
        seen["url"] = req.full_url  # type: ignore[attr-defined]
        seen["title"] = req.get_header("Title")  # type: ignore[attr-defined]
        seen["priority"] = req.get_header("Priority")  # type: ignore[attr-defined]

    monkeypatch.setattr(notify, "_post", fake_post)
    r = notify.notify("gate broken", "3 tests failed", level="crit")
    assert r.channels == ["ntfy"] and not r.errors
    assert seen["url"] == "https://ntfy.sh/zecor-test-abc"
    assert seen["title"] == "gate broken" and seen["priority"] == "5"


def test_a_push_failure_is_captured_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_NTFY_TOPIC", "x")

    def boom(_req: object) -> None:
        raise TimeoutError("slow network")

    monkeypatch.setattr(notify, "_post", boom)
    r = notify.notify("still fine", level="info")
    assert r.transcript_id > 0
    assert r.channels == [] and r.errors and "ntfy" in r.errors[0]


def test_self_hosted_url_beats_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_NTFY_URL", "https://push.example.com/alerts/")
    monkeypatch.setenv("ZECOR_NTFY_TOPIC", "ignored")
    assert notify._ntfy_target() == "https://push.example.com/alerts"


def test_configured_channels_lists_what_is_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    assert notify.configured_channels() == []
    monkeypatch.setenv("ZECOR_PUSHOVER_TOKEN", "t")
    monkeypatch.setenv("ZECOR_PUSHOVER_USER", "u")
    assert notify.configured_channels() == ["pushover"]