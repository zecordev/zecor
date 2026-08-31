# SPDX-License-Identifier: Apache-2.0
"""zecor.sandbox -- wrap a lane's gate command in `zecor-sandbox` when the mode wants
isolation and the Rust helper is built."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zecor import _rustbridge, modes, sandbox, toolgate


@pytest.fixture(autouse=True)
def _reset() -> None:
    _rustbridge.binary.cache_clear()
    modes.reset_cache()
    yield
    _rustbridge.binary.cache_clear()
    modes.reset_cache()


def _fake_binary(dirpath: Path) -> Path:
    exe = dirpath / ("zecor-sandbox.exe" if os.name == "nt" else "zecor-sandbox")
    exe.write_text("#!/bin/sh\nexec \"$@\"\n")
    exe.chmod(0o755)
    return exe


def test_wrap_is_a_noop_without_the_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_NO_RUST", "1")
    monkeypatch.setenv("ZECOR_MODE", "hardened")
    _rustbridge.binary.cache_clear()
    modes.reset_cache()
    cmd = ["/bin/sh", "-c", "true"]
    assert sandbox.wrap(cmd, workdir=tmp_path) == cmd


def test_wrap_is_a_noop_in_guarded_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZECOR_NO_RUST", raising=False)
    monkeypatch.setenv("ZECOR_RUST_BIN_DIR", str(tmp_path))
    monkeypatch.setenv("ZECOR_MODE", "guarded")
    _fake_binary(tmp_path)
    _rustbridge.binary.cache_clear()
    modes.reset_cache()
    assert not sandbox.wanted()
    cmd = ["/bin/sh", "-c", "true"]
    assert sandbox.wrap(cmd, workdir=tmp_path) == cmd


def test_wrap_engages_in_hardened_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZECOR_NO_RUST", raising=False)
    monkeypatch.setenv("ZECOR_RUST_BIN_DIR", str(tmp_path))
    monkeypatch.setenv("ZECOR_MODE", "hardened")
    exe = _fake_binary(tmp_path)
    _rustbridge.binary.cache_clear()
    modes.reset_cache()
    assert sandbox.wanted()

    wd = tmp_path / "lane"
    wd.mkdir()
    out = sandbox.wrap(["pytest", "-q"], workdir=wd)
    assert out[:4] == [str(exe), "run", "--policy", str(wd / ".zecor-sandbox.policy")]
    assert out[-2:] == ["pytest", "-q"] and out[4] == "--"

    pol = (wd / ".zecor-sandbox.policy").read_text()
    assert f"workdir = {wd}" in pol
    assert "allow_net = false" in pol
    assert str(wd) in pol


def test_wrap_force_ignores_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZECOR_NO_RUST", raising=False)
    monkeypatch.setenv("ZECOR_RUST_BIN_DIR", str(tmp_path))
    monkeypatch.setenv("ZECOR_MODE", "greenfield")  # writes_are_isolated_required, but...
    monkeypatch.setattr(sandbox, "wanted", lambda: False)
    _fake_binary(tmp_path)
    _rustbridge.binary.cache_clear()
    out = sandbox.wrap(["true"], workdir=tmp_path, force=True)
    assert out[1] == "run"


def test_allow_net_and_extra_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZECOR_NO_RUST", raising=False)
    monkeypatch.setenv("ZECOR_RUST_BIN_DIR", str(tmp_path))
    monkeypatch.setenv("ZECOR_MODE", "hardened")
    _fake_binary(tmp_path)
    _rustbridge.binary.cache_clear()
    modes.reset_cache()
    extra = tmp_path / "cache"
    sandbox.wrap(["true"], workdir=tmp_path, allow_write=[extra], allow_net=True)
    pol = (tmp_path / ".zecor-sandbox.policy").read_text()
    assert "allow_net = true" in pol
    assert str(extra) in pol


# -- toolgate: the wrapper is trusted, the wrapped argv is not ----------------


def test_toolgate_checks_the_wrapped_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_MODE", "hardened")
    modes.reset_cache()
    ok = toolgate.check(
        ["zecor-sandbox", "run", "--policy", "/x", "--", "pytest", "-q"]
    )
    assert ok.allowed
    bad = toolgate.check(
        ["zecor-sandbox", "run", "--policy", "/x", "--", "curl", "http://evil"]
    )
    assert not bad.allowed


def test_toolgate_rejects_a_wrapper_with_no_inner_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_MODE", "hardened")
    modes.reset_cache()
    assert not toolgate.check(["zecor-sandbox", "run", "--policy", "/x"]).allowed
    assert not toolgate.check(["zecor-sandbox", "run", "--policy", "/x", "--"]).allowed


def test_toolgate_allows_show(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_MODE", "hardened")
    modes.reset_cache()
    assert toolgate.check(["zecor-sandbox", "show", "--policy", "/x"]).allowed
