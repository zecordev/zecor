# SPDX-License-Identifier: Apache-2.0
"""Rule promotion. Plan section 7.4.

Mines landed diffs for a fix shape Zecor keeps repeating and, once one recurs
RULE_PROMOTE_AT times, drops a candidate rule in `rules/` for a human to confirm. It
does not execute rules -- that's the T0 applier; until confirmed, `route()` sends
`deterministic_fix_available` to a human.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from zecor.config import RULES_DIR

RULE_PROMOTE_AT = int(os.environ.get("ZECOR_RULE_PROMOTE_AT", "3"))
_MAX_EXAMPLES = 3
_SEP = "\x1e"

_LIT = re.compile(r"""("[^"]*"|'[^']*'|\b\d+\b)""")
_WS = re.compile(r"\s+")
_LONG_ID = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_KEYWORDS = frozenset(
    {
        "def", "class", "return", "raise", "if", "elif", "else", "for", "while",
        "try", "except", "finally", "with", "import", "from", "as", "pass", "lambda",
        "yield", "await", "async", "not", "and", "or", "is", "in",
        "None", "True", "False", "self",
    }
)


def _norm(line: str) -> str:
    line = _LIT.sub("<lit>", line.strip())
    line = _LONG_ID.sub(lambda m: m.group(0) if m.group(0) in _KEYWORDS else "ID", line)
    return _WS.sub(" ", line)


_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.spec\.", re.I
)
_MIN_SHAPE_LINES = 3  # a 1-line literal swap (timeout=10 -> 20) is too generic to promote


def _non_test_diff(diff: str) -> str:
    """Drop whole file sections whose path is a test file -- a recurring PRODUCTION
    fix shouldn't get a new signature every time its test scaffolding differs."""
    out: list[str] = []
    keep = True
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            path = parts[-1][2:] if len(parts) >= 4 else ""
            keep = not _TEST_PATH.search(path)
        if keep:
            out.append(line)
    return "\n".join(out)


def fix_signature(diff: str) -> str | None:
    """A stable hash of the SHAPE of the PRODUCTION part of a diff: literals masked,
    non-keyword identifiers collapsed to `ID`, whitespace flattened. None for a change
    that is test-only, pure noise, or too small to be a meaningful recurring shape."""
    removed: list[str] = []
    added: list[str] = []
    for raw in _non_test_diff(diff).splitlines():
        if raw.startswith(("+++", "---", "diff ", "index ", "@@")):
            continue
        if raw.startswith("-") and raw[1:].strip():
            removed.append(_norm(raw[1:]))
        elif raw.startswith("+") and raw[1:].strip():
            added.append(_norm(raw[1:]))
    removed = [x for x in removed if x and not x.startswith("#")]
    added = [x for x in added if x and not x.startswith("#")]
    if len(removed) + len(added) < _MIN_SHAPE_LINES:
        return None
    key = "R:" + "|".join(sorted(removed)) + "\nA:" + "|".join(sorted(added))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class ShapeStore:
    """The `fix_shape` table, wrapped. Kept separate from Ledger so a schema without
    the table degrades to a no-op instead of crashing the land path."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.ok = self._has_table()

    def _has_table(self) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fix_shape'"
        ).fetchone()
        return row is not None

    def record(self, sig: str, example: str) -> tuple[int, bool]:
        """Atomically bump the count for `sig`, keeping up to 3 example diffs. Returns
        (new_count, should_promote_now); should_promote stays true until a candidate
        file is actually written, so a failed write at the threshold retries on the
        next recurrence."""
        if not self.ok:
            return 0, False
        now = datetime.now(UTC).isoformat()
        excerpt = example.strip().replace(_SEP, " ")[:1200]
        # UPSERT: count += 1; append the example if room; first_seen only on insert.
        self.db.execute(
            "INSERT INTO fix_shape (sig, count, first_seen, last_seen, examples, promoted) "
            "VALUES (?, 1, ?, ?, ?, 0) "
            "ON CONFLICT(sig) DO UPDATE SET "
            "  count = count + 1, last_seen = excluded.last_seen, "
            "  examples = CASE "
            "    WHEN instr(examples, excluded.examples) > 0 THEN examples "
            "    WHEN (length(examples) - length(replace(examples, char(30), ''))) >= ? "
            "      THEN examples "
            "    ELSE examples || char(30) || excluded.examples END",
            (sig, now, now, excerpt, _MAX_EXAMPLES - 1),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT count, promoted FROM fix_shape WHERE sig = ?", (sig,)
        ).fetchone()
        count = int(row["count"])
        return count, count >= RULE_PROMOTE_AT and not int(row["promoted"])

    def mark_promoted(self, sig: str) -> None:
        if self.ok:
            self.db.execute("UPDATE fix_shape SET promoted = 1 WHERE sig = ?", (sig,))
            self.db.commit()

    def examples(self, sig: str) -> list[str]:
        if not self.ok:
            return []
        row = self.db.execute(
            "SELECT examples FROM fix_shape WHERE sig = ?", (sig,)
        ).fetchone()
        return [e for e in ((row["examples"] if row else "") or "").split(_SEP) if e]


def note_landed_fix(
    db: sqlite3.Connection, card_intent: str, diff: str, *, rules_dir: Path | None = None
) -> str | None:
    """Call after a land. Returns a one-line note if a candidate rule was written."""
    sig = fix_signature(diff)
    if sig is None:
        return None
    store = ShapeStore(db)
    count, crossed = store.record(sig, diff)
    if not crossed:
        return f"fix-shape {sig} seen {count}x" if count > 1 else None
    path = _write_candidate(sig, card_intent, store.examples(sig), rules_dir or RULES_DIR)
    store.mark_promoted(sig)
    return f"fix-shape {sig} recurred {count}x -- candidate rule: {path}"


def _write_candidate(sig: str, intent: str, examples: list[str], rules_dir: Path) -> Path:
    rules_dir.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"[0-9a-f]{16}", sig):  # sig is a sha slice -- no path escape
        raise ValueError(f"bad rule signature {sig!r}")
    path = rules_dir / f"candidate-{sig}.md"
    if path.exists():
        return path  # another lane already wrote it
    body = [
        f"# Candidate rule {sig}",
        "",
        f"Auto-mined: this fix shape recurred {RULE_PROMOTE_AT}x. Confirm it is a real",
        "deterministic transform, turn it into an ast-grep / codemod rule, and delete this",
        "file. Until then `route()` sends matching cards to a human, not T0.",
        "",
        f"Most recent task: {intent}",
        "",
        "## Example diffs",
        "",
    ]
    for i, ex in enumerate(examples, 1):
        body += [f"### {i}", "```diff", ex, "```", ""]
    fd, tmp = tempfile.mkstemp(dir=str(rules_dir), prefix=f".candidate-{sig}.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(body))
    os.replace(tmp, path)  # atomic; last writer wins but the content is identical
    return path