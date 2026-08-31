# SPDX-License-Identifier: Apache-2.0
"""Structural code analysis over a change set, before it reaches a model.

Backed by the `zecor-astscan` Rust crate (Tree-sitter): `syntax_errors()` catches a
model that left a file that won't parse, and `symbols()` lists top-level definitions
across Python/JS/TS/Rust/Go. Python has a stdlib fallback (`compile()`/`ast`) so the
check still runs without the Rust helper built; other languages need it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from zecor import _rustbridge

_SRC_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".mts", ".cts", ".tsx", ".rs", ".go",
}


@dataclass(frozen=True)
class SyntaxIssue:
    file: str
    row: int
    kind: str  # error | missing
    text: str


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    start_byte: int
    end_byte: int
    row: int


def is_source(path: str) -> bool:
    return Path(path).suffix.lower() in _SRC_EXT


def syntax_errors(root: Path, files: list[str]) -> list[SyntaxIssue]:
    """Every syntax error across `files` (relative to `root`). Empty == all parse."""
    paths = [str(root / f) for f in files if is_source(f) and (root / f).is_file()]
    if not paths:
        return []
    obj = _rustbridge.run_json("zecor-astscan", ["check", *paths])
    if isinstance(obj, list):
        out: list[SyntaxIssue] = []
        for r in obj:
            if not isinstance(r, dict) or r.get("ok"):
                continue
            rel = _rel(str(r.get("file", "")), root)
            for e in r.get("errors", []) or []:
                out.append(SyntaxIssue(rel, int(e.get("row", 0)), str(e.get("kind", "error")),
                                       str(e.get("text", ""))))
        return out
    # fallback: Python only
    out = []
    for f in files:
        p = root / f
        if p.suffix.lower() not in (".py", ".pyi") or not p.is_file():
            continue
        try:
            compile(p.read_text(), f, "exec")
        except SyntaxError as e:
            out.append(SyntaxIssue(f, e.lineno or 0, "error", (e.msg or "syntax error")[:80]))
    return out


def symbols(root: Path, file: str) -> list[Symbol]:
    p = root / file
    if not p.is_file():
        return []
    obj = _rustbridge.run_json("zecor-astscan", ["symbols", str(p)])
    if isinstance(obj, list):
        return [
            Symbol(str(d["name"]), str(d["kind"]), int(d["start_byte"]),
                   int(d["end_byte"]), int(d["row"]))
            for d in obj if isinstance(d, dict)
        ]
    if p.suffix.lower() in (".py", ".pyi"):
        return _py_symbols(p.read_text())
    return []


def _py_symbols(source: str) -> list[Symbol]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            kind = "function"
        elif isinstance(node, ast.ClassDef):
            kind = "class"
        else:
            continue
        seg = ast.get_source_segment(source, node) or ""
        start = source.find(seg) if seg else 0
        out.append(Symbol(node.name, kind, max(0, start), start + len(seg), node.lineno))
    return out


@dataclass(frozen=True)
class FnMetric:
    name: str
    kind: str
    row: int
    lines: int
    complexity: int
    max_depth: int
    params: int


@dataclass(frozen=True)
class SymbolChange:
    change: str  # added | removed | modified
    kind: str
    name: str
    old_row: int | None
    new_row: int | None


def metrics(root: Path, file: str) -> list[FnMetric]:
    """Per top-level function: line count, McCabe complexity, nesting depth, arity.
    Rust-only (no Python fallback); empty list if the helper is not built."""
    p = root / file
    if not p.is_file():
        return []
    obj = _rustbridge.run_json("zecor-astscan", ["metrics", str(p)])
    if not isinstance(obj, list):
        return []
    out: list[FnMetric] = []
    for fm in obj:
        if not isinstance(fm, dict):
            continue
        for fn in fm.get("functions", []) or []:
            out.append(FnMetric(
                str(fn["name"]), str(fn["kind"]), int(fn["row"]), int(fn["lines"]),
                int(fn["complexity"]), int(fn["max_depth"]), int(fn["params"]),
            ))
    return out


def structural_diff(old_path: Path, new_path: Path) -> list[SymbolChange]:
    """Which top-level symbols were added / removed / had their body change between two
    versions of a file. Whitespace-only edits are not reported. Rust-only."""
    obj = _rustbridge.run_json("zecor-astscan", ["diff", str(old_path), str(new_path)])
    if not isinstance(obj, list):
        return []
    out: list[SymbolChange] = []
    for d in obj:
        if not isinstance(d, dict):
            continue
        out.append(SymbolChange(
            str(d["change"]), str(d["kind"]), str(d["name"]),
            d.get("old_row"), d.get("new_row"),
        ))
    return out


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return path
