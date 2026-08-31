# SPDX-License-Identifier: Apache-2.0
"""Deterministic secret scan for staged diffs and generated PR text.

Runs before any git write on the land path: known-pattern matches plus a
Shannon-entropy check on long unbroken tokens. A hit blocks the write; findings name
the file and line but never echo the secret. The `zecor-secretscan` Rust tool
implements the same rules faster; when installed, `scan_text`/`scan_diff` route
through it, with the Python below as the byte-for-byte fallback and reference.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from zecor import _rustbridge

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("aws-secret-access-key", re.compile(r"(?i)aws_secret[^=]{0,20}=\s*['\"]?[A-Za-z0-9/+=]{40}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("github-pat-fine", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("gitlab-pat", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/T[A-Za-z0-9_/]{40,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("gcp-sa-key", re.compile(r'"type"\s*:\s*"service_account"')),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
    ("stripe-secret-key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{24,}\b")),
    ("twilio-key", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi-token", re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{50,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("db-connection-uri", re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:/@]+:[^\s:/@]{3,}@[^\s/]+"
    )),
    ("private-key-block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
    )),
    ("generic-assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passwd|password|token|client[_-]?secret|access[_-]?token)\b"
        r"\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"
    )),
)

_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")
_ENTROPY_MIN = 4.0  # bits/char; base64-ish random material sits ~4.5-6
_ALLOW_SUBSTR = ("example", "changeme", "your-", "xxxx", "placeholder", "redacted", "dummy",
                 "notreal", "test-value")


@dataclass(frozen=True)
class Finding:
    rule: str
    file: str
    line: int
    hint: str  # a masked excerpt -- never the secret


def _shannon(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _mask(tok: str) -> str:
    return tok[:4] + "…" + tok[-2:] if len(tok) > 8 else "***"


def _is_placeholder(token: str) -> bool:
    t = token.lower()
    return any(a in t for a in _ALLOW_SUBSTR)


def _findings_from_json(obj: object) -> list[Finding] | None:
    if not isinstance(obj, list):
        return None
    out: list[Finding] = []
    for d in obj:
        if not isinstance(d, dict):
            return None
        out.append(Finding(str(d["rule"]), str(d["file"]), int(d["line"]), str(d["hint"])))
    return out


def scan_text(text: str, *, file: str = "<text>") -> list[Finding]:
    rust = _findings_from_json(_rustbridge.run_json("zecor-secretscan", ["text"], text))
    if rust is not None:
        return [Finding(f.rule, file, f.line, f.hint) for f in rust]
    return _scan_text_py(text, file=file)


def _scan_text_py(text: str, *, file: str = "<text>") -> list[Finding]:
    out: list[Finding] = []
    for i, line in enumerate(text.splitlines(), 1):
        for rule, pat in _PATTERNS:
            m = pat.search(line)
            # allow-list the MATCHED token, never the whole line -- `REAL_KEY # example`
            # must not slip through
            if m and not _is_placeholder(m.group(0)):
                out.append(Finding(rule, file, i, _mask(m.group(0))))
        for m in _ENTROPY_TOKEN.finditer(line):
            tok = m.group(0)
            if _shannon(tok) >= _ENTROPY_MIN and not _is_placeholder(tok):
                out.append(Finding("high-entropy-token", file, i, _mask(tok)))
    return out


def scan_diff(diff: str) -> list[Finding]:
    """Scan only added lines of a unified diff."""
    rust = _findings_from_json(_rustbridge.run_json("zecor-secretscan", ["diff"], diff))
    if rust is not None:
        return rust
    return _scan_diff_py(diff)


def _scan_diff_py(diff: str) -> list[Finding]:
    out: list[Finding] = []
    cur = "<diff>"
    lineno = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            cur = name[2:] if name.startswith("b/") else name  # strip exactly the `b/` prefix
            lineno = 0
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) - 1 if m else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            lineno += 1
            for f in _scan_text_py(raw[1:], file=cur):
                out.append(Finding(f.rule, cur, lineno, f.hint))
        elif not raw.startswith("-"):
            lineno += 1
    return out


def clean(text_or_diff: str, *, is_diff: bool = False) -> tuple[bool, list[Finding]]:
    findings = scan_diff(text_or_diff) if is_diff else scan_text(text_or_diff)
    return (not findings, findings)


_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = (1 << 64) - 1


def fingerprint(f: Finding) -> str:
    """Line-independent id for baselining -- must match the Rust `Finding::fingerprint`."""
    h = _FNV_OFFSET
    for part in (f.rule, "\0", f.file, "\0", f.hint):
        for b in part.encode():
            h = ((h ^ b) * _FNV_PRIME) & _FNV_MASK
    return f"{h:016x}"


def scan_tree(
    root: str, *, injection: bool = False, respect_gitignore: bool = True
) -> list[Finding]:
    """Walk a directory (gitignore-aware, parallel in Rust) and scan every text file.
    Falls back to a simple `os.walk` when the Rust helper is not built."""
    args = ["path", root]
    if injection:
        args.append("--injection")
    if not respect_gitignore:
        args.append("--no-gitignore")
    rust = _findings_from_json(_rustbridge.run_json("zecor-secretscan", args, ""))
    if rust is not None:
        return rust
    # note: the Python fallback has no injection pack
    return _scan_tree_py(root, respect_gitignore=respect_gitignore)


# Directories skipped unconditionally -- not gitignore entries, so they're excluded
# even when a repo has no .gitignore of its own.
_ALWAYS_SKIP_DIRS = frozenset({".git", "node_modules", "target", ".venv", "venv"})


def _load_gitignore(dirpath: str) -> list[tuple[str, bool]]:
    """(pattern, dir_only) pairs from a directory's own .gitignore. No negation
    (`!pattern`) or `**` support -- the common cases only, not the full spec."""
    import os

    p = os.path.join(dirpath, ".gitignore")
    try:
        lines = open(p, encoding="utf-8", errors="replace").read().splitlines()  # noqa: SIM115
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        dir_only = line.endswith("/")
        out.append((line.rstrip("/").lstrip("/"), dir_only))
    return out


def _gitignore_matches(name: str, *, is_dir: bool, patterns: list[tuple[str, bool]]) -> bool:
    import fnmatch

    for pat, dir_only in patterns:
        if dir_only and not is_dir:
            continue
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _scan_tree_py(root: str, *, respect_gitignore: bool = True) -> list[Finding]:
    import os

    out: list[Finding] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _ALWAYS_SKIP_DIRS]
        if respect_gitignore:
            patterns = _load_gitignore(dirpath)
            if patterns:
                dirnames[:] = [
                    d for d in dirnames if not _gitignore_matches(d, is_dir=True, patterns=patterns)
                ]
                filenames = [
                    f for f in filenames
                    if not _gitignore_matches(f, is_dir=False, patterns=patterns)
                ]
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                if os.path.getsize(p) > 2 * 1024 * 1024:
                    continue
                raw = open(p, "rb").read()  # noqa: SIM115
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue
            rel = os.path.relpath(p, root)
            out.extend(Finding(f.rule, rel, f.line, f.hint)
                       for f in _scan_text_py(raw.decode("utf-8", "replace"), file=rel))
    out.sort(key=lambda f: (f.file, f.line, f.rule))
    return out
