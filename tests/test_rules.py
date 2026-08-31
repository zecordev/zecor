# SPDX-License-Identifier: Apache-2.0
"""Rule promotion: mine recurring fix shapes. Plan section 7.4."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zecor import rules
from zecor.rules import ShapeStore, fix_signature, note_landed_fix

SCHEMA = Path(__file__).resolve().parents[1] / "setup" / "schema.sql"

DIFF_A = """diff --git a/kvstore/store.py b/kvstore/store.py
@@ -14,3 +14,4 @@ class KVStore:
     def delete(self, key):
-        raise NotImplementedError("not yet")
+        if key not in self._data:
+            raise KeyError(key)
+        del self._data[key]
"""
DIFF_B = """diff --git a/other/thing.py b/other/thing.py
@@ -1,2 +1,3 @@ class Thing:
     def remove(self, name):
-        raise NotImplementedError("todo")
+        if name not in self._items:
+            raise KeyError(name)
+        del self._items[name]
"""
DIFF_UNRELATED = """diff --git a/x.py b/x.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA.read_text())
    return db


def test_signature_is_stable_across_renamed_identifiers() -> None:
    # same SHAPE (delete a NotImplementedError, add a KeyError guard + del) -> same sig
    assert fix_signature(DIFF_A) == fix_signature(DIFF_B)


def test_signature_differs_for_a_different_change() -> None:
    assert fix_signature(DIFF_A) != fix_signature(DIFF_UNRELATED)


def test_pure_noise_diff_has_no_signature() -> None:
    assert fix_signature("diff --git a/x b/x\n@@ @@\n-  \n+   \n") is None
    assert fix_signature("@@ @@\n-# a comment\n+# another comment\n") is None


def test_candidate_rule_is_written_on_the_third_recurrence(tmp_path: Path) -> None:
    db = _db()
    rd = tmp_path / "rules"
    n1 = note_landed_fix(db, "fix delete", DIFF_A, rules_dir=rd)
    n2 = note_landed_fix(db, "fix remove", DIFF_B, rules_dir=rd)
    assert n1 is None  # first sighting, nothing to say
    assert n2 and "2x" in n2 and "candidate" not in n2
    n3 = note_landed_fix(db, "fix drop", DIFF_A, rules_dir=rd)
    assert n3 and "candidate rule" in n3
    files = list(rd.glob("candidate-*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "Example diffs" in body and "raise" in body


def test_promotion_happens_once(tmp_path: Path) -> None:
    db = _db()
    rd = tmp_path / "rules"
    for intent in ("a", "b", "c", "d", "e"):
        note_landed_fix(db, intent, DIFF_A, rules_dir=rd)
    assert len(list(rd.glob("candidate-*.md"))) == 1  # not re-written on 4th/5th


def test_missing_table_is_a_silent_noop() -> None:
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    store = ShapeStore(bare)
    assert store.ok is False
    assert store.record("sig", "diff") == (0, False)


def test_promote_at_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules, "RULE_PROMOTE_AT", 1)
    db = _db()
    n = note_landed_fix(db, "one shot", DIFF_A, rules_dir=tmp_path / "r")
    assert n and "candidate rule" in n