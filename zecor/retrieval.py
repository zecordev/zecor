# SPDX-License-Identifier: Apache-2.0
"""Retrieval -- build-queue item E.

Fill a card's `allow_files` and feed the prompt real context, without a frontier call,
in two layers, cheap first: a deterministic symbol index (stdlib `ast`) mapping every
module-level name to the files defining it, then an optional embedding rerank (LM
Studio, `MODEL_EMBED`) by cosine similarity over the files the lexical layer couldn't
pin -- if the embed model is down, the lexical result stands. The index is cached under
`ZECOR_ROOT/.cache/` keyed by a cheap repo signature, so a plan run doesn't re-parse a
large tree every time.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import math
import re
from pathlib import Path

from zecor import config

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", "dist", "build",
}
_MAX_EMBED_FILES = 200  # above this the embed pass is skipped -- too slow to be worth it


def _py_files(repo_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in repo_dir.rglob("*.py"):
        if not any(part in _SKIP_DIRS for part in p.parts):
            out.append(p)
    return sorted(out)


def _signature(repo_dir: Path) -> str:
    files = _py_files(repo_dir)
    newest = max((f.stat().st_mtime for f in files), default=0.0)
    key = f"{repo_dir}|{len(files)}|{newest:.0f}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _cache_path(sig: str) -> Path:
    d = config.ZECOR_ROOT / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"symbols-{sig}.json"


def _module_names(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def symbol_index(repo_dir: Path) -> dict[str, list[str]]:
    """{symbol name -> [relative file path, ...]}. Cached on the repo signature."""
    sig = _signature(repo_dir)
    cache = _cache_path(sig)
    if cache.exists():
        try:
            loaded = json.loads(cache.read_text())
            if isinstance(loaded, dict):
                return {k: list(v) for k, v in loaded.items()}
        except (OSError, json.JSONDecodeError):
            pass
    index: dict[str, list[str]] = {}
    for f in _py_files(repo_dir):
        try:
            rel = str(f.relative_to(repo_dir))
            for name in _module_names(f.read_text()):
                index.setdefault(name, [])
                if rel not in index[name]:
                    index[name].append(rel)
        except OSError:
            continue
    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(index))
    return index


def _names_in(text: str) -> list[str]:
    """Identifier-ish tokens from the task text, plus split CamelCase / snake_case."""
    raw = set(_IDENT.findall(text))
    extra: set[str] = set()
    for tok in raw:
        extra.update(p for p in re.split(r"[_]+", tok) if len(p) > 2)
        extra.update(p for p in re.findall(r"[A-Z][a-z]+|[a-z]+", tok) if len(p) > 2)
    return sorted(raw | extra)


def lexical_files(repo_dir: Path, intent: str, *, k: int = 3) -> list[str]:
    """Files that define a symbol named in the task text, ranked by how many such
    symbols they hold."""
    index = symbol_index(repo_dir)
    hits: dict[str, int] = {}
    for name in _names_in(intent):
        for path in index.get(name, []):
            hits[path] = hits.get(path, 0) + 1
    return [p for p, _ in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))][:k]


def _blob(repo_dir: Path, rel: str) -> str:
    try:
        src = (repo_dir / rel).read_text()
    except OSError:
        return rel
    try:
        tree = ast.parse(src)
        doc = ast.get_docstring(tree) or ""
        names = " ".join(_module_names(src))
    except (SyntaxError, ValueError):
        doc, names = "", ""
    return f"{rel}\n{doc}\n{names}"[:2000]


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def embed_rank(repo_dir: Path, intent: str, candidates: list[str], *, k: int = 3) -> list[str]:
    """Cosine-rank `candidates` against the task text. Falls back to `candidates[:k]`
    unchanged if the embed model is unavailable."""
    cands = candidates[:_MAX_EMBED_FILES]
    if not cands:
        return []
    try:
        from zecor.localmodel import embed

        vecs = embed([intent, *(_blob(repo_dir, c) for c in cands)])
    except Exception:  # embeddings are strictly optional; lexical result stands
        return cands[:k]
    if len(vecs) != len(cands) + 1:
        return cands[:k]
    q, rest = vecs[0], vecs[1:]
    scored = sorted(zip(cands, rest, strict=True), key=lambda cr: -_cos(q, cr[1]))
    return [c for c, _v in scored][:k]


def suggest(repo_dir: Path, intent: str, *, k: int = 3) -> list[str]:
    """Best guess at the files a task should be allowed to touch: exact symbol hits
    first, then an embedding rerank of the rest of the tree to fill up to `k`."""
    lex = lexical_files(repo_dir, intent, k=k)
    if len(lex) >= k:
        return lex
    seen = set(lex)
    others = [
        rel for f in _py_files(repo_dir) if (rel := str(f.relative_to(repo_dir))) not in seen
    ]
    filled = embed_rank(repo_dir, intent, others, k=k - len(lex))
    return [*lex, *[f for f in filled if f not in seen]][:k]