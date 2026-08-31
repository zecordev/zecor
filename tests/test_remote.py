# SPDX-License-Identifier: Apache-2.0
"""zecor.remote: the phone-path setup guide."""

from __future__ import annotations

import pytest

from zecor import remote


def test_connection_string_shape() -> None:
    s = remote.connection_string("mac.tail1234.ts.net")
    assert s == "mosh mac.tail1234.ts.net -- tmux new -A -s cc 'zecor console'"


def test_plan_stops_at_missing_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "_which", lambda c: False)
    text, code = remote.plan(do_install=False)
    assert code == 1
    assert "brew install tailscale" in text and "brew install mosh" in text


def test_plan_stops_at_tailscale_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "_which", lambda c: True)
    monkeypatch.setattr(
        remote, "status",
        lambda: remote.RemoteStatus(True, True, True, up=False, dns_name="", ipv4=""),
    )
    text, code = remote.plan(do_install=False)
    assert code == 1 and "tailscale up" in text


def test_plan_prints_the_recipe_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "_which", lambda c: c != "qrencode")
    monkeypatch.setattr(
        remote, "status",
        lambda: remote.RemoteStatus(True, True, True, up=True,
                                    dns_name="mac.tail1234.ts.net", ipv4="100.64.0.1"),
    )
    text, code = remote.plan(do_install=False)
    assert code == 0
    assert "mosh mac.tail1234.ts.net -- tmux new -A -s cc 'zecor console'" in text
    assert "mac.tail1234.ts.net" in text