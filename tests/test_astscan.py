# SPDX-License-Identifier: Apache-2.0
"""zecor.astscan -- Tree-sitter syntax check + symbol extraction, Rust-backed."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import _rustbridge, astscan


def test_python_fallback_syntax_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_NO_RUST", "1")
    _rustbridge.binary.cache_clear()
    (tmp_path / "good.py").write_text("def f():\n    return 1\n")
    (tmp_path / "bad.py").write_text("def f(:\n    return\n")
    issues = astscan.syntax_errors(tmp_path, ["good.py", "bad.py"])
    assert [i.file for i in issues] == ["bad.py"]
    _rustbridge.binary.cache_clear()


def test_python_fallback_symbols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_NO_RUST", "1")
    _rustbridge.binary.cache_clear()
    (tmp_path / "m.py").write_text("import os\n\ndef a(): pass\n\nclass B: pass\n")
    names = [s.name for s in astscan.symbols(tmp_path, "m.py")]
    assert names == ["a", "B"]
    _rustbridge.binary.cache_clear()


def test_rust_backed_multilang(tmp_path: Path) -> None:
    if _rustbridge.binary("zecor-astscan") is None:
        pytest.skip("zecor-astscan not built")
    (tmp_path / "a.ts").write_text("export function f( { return\n")  # broken
    (tmp_path / "b.ts").write_text("export function g(): void {}\n")
    issues = astscan.syntax_errors(tmp_path, ["a.ts", "b.ts"])
    assert {i.file for i in issues} == {"a.ts"}
    (tmp_path / "c.rs").write_text("pub fn h() {}\nstruct S;\n")
    kinds = {s.kind for s in astscan.symbols(tmp_path, "c.rs")}
    assert {"function", "struct"} <= kinds


def test_non_source_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("# not code\n")
    assert astscan.syntax_errors(tmp_path, ["readme.md"]) == []


def test_metrics(tmp_path: Path) -> None:
    if _rustbridge.binary("zecor-astscan") is None:
        pytest.skip("zecor-astscan not built")
    (tmp_path / "m.py").write_text(
        "def simple(a, b):\n    return a + b\n\n"
        "def gnarly(x):\n"
        "    if x and x > 0:\n"
        "        for i in range(x):\n"
        "            if i % 2:\n"
        "                return i\n"
        "    return 0\n"
    )
    fns = {f.name: f for f in astscan.metrics(tmp_path, "m.py")}
    assert fns["simple"].complexity == 1
    assert fns["simple"].params == 2
    assert fns["gnarly"].complexity >= 4
    assert fns["gnarly"].max_depth >= 3


def test_structural_diff(tmp_path: Path) -> None:
    if _rustbridge.binary("zecor-astscan") is None:
        pytest.skip("zecor-astscan not built")
    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    old.write_text("def a():\n    return 1\n\ndef b():\n    return 2\n")
    new.write_text(
        "def a():\n        return 1\n\ndef b():\n    return 99\n\ndef c():\n    return 3\n"
    )
    ch = {c.name: c.change for c in astscan.structural_diff(old, new)}
    assert ch == {"b": "modified", "c": "added"}  # `a` only reindented
