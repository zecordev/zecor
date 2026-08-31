# SPDX-License-Identifier: Apache-2.0
"""Deep self-review: the gate-quality revert probe and the agreement filter."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.fakes import make_repo
from zecor import selfreview
from zecor.cards import Card
from zecor.review import Finding, ReviewResult
from zecor.selfreview import (
    _changed_new_lines,
    _corroborates,
    _module_symbols,
    deep_review,
    gate_quality,
    should_deep_review,
    symbol_scope_findings,
)
from zecor.task import Klass


def card(verify: str = "./.fleet/verify.sh", files: list[str] | None = None) -> Card:
    return Card("0001", "demo", "fix the thing", verify, files or ["src.txt"])


def test_should_deep_review_triggers() -> None:
    assert should_deep_review(card(), ["t1"], files_changed=2) is True  # multi-file
    assert should_deep_review(card(), ["t1", "t3"], files_changed=1) is True  # escalated
    assert should_deep_review(card("pytest -q tests/test_x.py"), ["t1"], 1) is True  # narrow
    assert should_deep_review(card('pytest -k "login or auth"'), ["t1"], 1) is True  # -k
    assert should_deep_review(card("./.fleet/verify.sh"), ["t1"], 1) is False


def _lane_with_change(tmp_path: Path, *, gate: str, real_fix: bool):
    """A committed lane: src.txt gets FIXED; the repo gate is `gate`."""
    repo = make_repo(tmp_path / "repo", verify_body=gate)
    lane_dir = tmp_path / "lane"
    subprocess.run(["git", "worktree", "add", "-b", "zecor/0001", str(lane_dir), "HEAD"],
                   cwd=repo, check=True, capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=lane_dir,
                          capture_output=True, text=True).stdout.strip()
    (lane_dir / "src.txt").write_text("// FIXED\n" if real_fix else "// v0\n")
    subprocess.run(["git", "add", "-A"], cwd=lane_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "fix"], cwd=lane_dir, check=True, capture_output=True)
    diff = subprocess.run(["git", "diff", f"{base}..HEAD"], cwd=lane_dir,
                          capture_output=True, text=True).stdout
    return lane_dir, base, diff


def test_gate_quality_flags_a_tautological_gate(tmp_path: Path) -> None:
    # gate is `exit 0` -- it passes with OR without the fix. That must be caught.
    lane_dir, base, diff = _lane_with_change(tmp_path, gate="exit 0", real_fix=True)
    f = gate_quality("./.fleet/verify.sh", lane_dir, base, diff)
    assert f is not None
    assert f.severity == "high" and "implementation reverted" in f.summary
    # the worktree was restored
    assert (lane_dir / "src.txt").read_text() == "// FIXED\n"


def test_gate_quality_passes_a_real_gate(tmp_path: Path) -> None:
    # gate is green iff src.txt contains FIXED -- reverting the fix makes it red.
    lane_dir, base, diff = _lane_with_change(
        tmp_path, gate="grep -q FIXED src.txt", real_fix=True
    )
    assert gate_quality("./.fleet/verify.sh", lane_dir, base, diff) is None
    assert (lane_dir / "src.txt").read_text() == "// FIXED\n"  # restored


def test_gate_quality_restores_even_when_the_gate_errors(tmp_path: Path) -> None:
    lane_dir, base, diff = _lane_with_change(
        tmp_path, gate="test -f src.txt && grep -q FIXED src.txt", real_fix=True
    )
    gate_quality("./.fleet/verify.sh", lane_dir, base, diff)
    assert (lane_dir / "src.txt").read_text() == "// FIXED\n"


def test_corroborates_needs_same_file_and_shared_keyword() -> None:
    a = Finding("adversarial", "medium", "src/a.py", 10, "off by one in the loop bound")
    hit = Finding("tests", "low", "src/a.py", 9, "loop bound looks wrong")
    miss_file = Finding("tests", "low", "src/b.py", 9, "loop bound looks wrong")
    miss_topic = Finding("tests", "low", "src/a.py", 9, "missing docstring")
    assert _corroborates(a, [hit]) is True
    assert _corroborates(a, [miss_file]) is False
    assert _corroborates(a, [miss_topic]) is False


def test_deep_review_agreement_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lane_dir, base, diff = _lane_with_change(
        tmp_path, gate="grep -q FIXED src.txt", real_fix=True
    )
    monkeypatch.setattr(
        selfreview, "review_branch",
        lambda c, d, **k: ReviewResult(verdict="ship", findings=[]),
    )
    # adversarial pass returns one low (dropped -- uncorroborated) and one high (kept)
    monkeypatch.setattr(
        selfreview, "ask",
        lambda p, **k: (
            '{"findings": ['
            '{"severity":"low","file":"src.txt","line":1,"summary":"nit only"},'
            '{"severity":"high","file":"src.txt","line":2,"summary":"drops the last item"}]}',
            "t4",
        ),
    )
    r = deep_review(card(), diff, lane_path=lane_dir, base_sha=base, ledger_has_frontier=True)
    kept = {f.summary for f in r.findings}
    assert "drops the last item" in kept
    assert "nit only" not in kept
    assert r.verdict == "needs-work"  # a kept high downgrades a "ship"


def test_deep_review_blocks_on_a_weak_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lane_dir, base, diff = _lane_with_change(tmp_path, gate="exit 0", real_fix=True)
    monkeypatch.setattr(
        selfreview, "review_branch", lambda c, d, **k: ReviewResult(verdict="ship")
    )
    monkeypatch.setattr(selfreview, "ask", lambda p, **k: ('{"findings": []}', "t4"))
    r = deep_review(card(), diff, lane_path=lane_dir, base_sha=base, ledger_has_frontier=True)
    assert r.verdict == "block"
    assert any(f.lens == "gate-quality" for f in r.findings)


def test_deep_review_flags_out_of_scope_files(tmp_path, monkeypatch) -> None:
    lane_dir, base, diff = _lane_with_change(
        tmp_path, gate="grep -q FIXED src.txt", real_fix=True
    )
    monkeypatch.setattr(
        selfreview, "review_branch", lambda c, d, **k: ReviewResult(verdict="ship")
    )
    monkeypatch.setattr(selfreview, "ask", lambda p, **k: ('{"findings": []}', "t4"))
    c = card(files=["some/other/file.py"])  # the diff touches src.txt, not this
    r = deep_review(c, diff, lane_path=lane_dir, base_sha=base, ledger_has_frontier=True)
    assert any(f.lens == "scope" and f.file == "src.txt" for f in r.findings)
    assert r.verdict in ("needs-work", "block")


def test_gate_quality_flags_an_unresolvable_base(tmp_path) -> None:
    lane_dir, _base, diff = _lane_with_change(
        tmp_path, gate="grep -q FIXED src.txt", real_fix=True
    )
    f = gate_quality("./.fleet/verify.sh", lane_dir, "deadbeefdeadbeef", diff)
    assert f is not None and f.severity == "medium" and "base commit" in f.summary


def test_gate_quality_keeps_the_new_test_when_reverting_the_impl(tmp_path: Path) -> None:
    # test-first shape: the diff adds tests/test_f.py (asserts feat()) AND src.py (feat()).
    # Reverting only src.py must leave the new test FAILING -> not a tautology.
    repo = make_repo(tmp_path / "repo", verify_body="python3 -m pytest -q")
    (repo / "src.py").write_text("def feat():\n    return None\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("")
    import subprocess as sp
    sp.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    sp.run(["git", "commit", "-qm", "base"], cwd=repo, check=True, capture_output=True)
    lane = tmp_path / "lane"
    sp.run(["git", "worktree", "add", "-b", "zecor/0001", str(lane), "HEAD"],
           cwd=repo, check=True, capture_output=True)
    base = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=lane, capture_output=True, text=True
    ).stdout.strip()
    (lane / "tests" / "test_f.py").write_text(
        "from src import feat\ndef test_feat():\n    assert feat() == 42\n"
    )
    sp.run(["git", "add", "-A"], cwd=lane, check=True, capture_output=True)
    sp.run(["git", "commit", "-qm", "test"], cwd=lane, check=True, capture_output=True)
    (lane / "src.py").write_text("def feat():\n    return 42\n")
    sp.run(["git", "add", "-A"], cwd=lane, check=True, capture_output=True)
    sp.run(["git", "commit", "-qm", "impl"], cwd=lane, check=True, capture_output=True)
    diff = sp.run(["git", "diff", f"{base}..HEAD"], cwd=lane, capture_output=True, text=True).stdout
    assert gate_quality("python3 -m pytest -q", lane, base, diff) is None  # NOT a tautology


# -- within-file scope creep (decision 5) ------------------------------------------

def test_changed_new_lines_tracks_offsets_across_a_deletion() -> None:
    diff = (
        "--- a/m.py\n+++ b/m.py\n"
        "@@ -1,4 +1,5 @@\n context\n-old\n+new1\n+new2\n more\n"
    )
    got = _changed_new_lines(diff)
    assert got["m.py"] == {2, 3}  # deletion sits at new-offset 2; new1@2, new2@3


def test_module_symbols_spans_defs_classes_and_assignments() -> None:
    src = "A = 1\n\ndef f():\n    return A\n\nclass C:\n    def m(self):\n        pass\n"
    syms = dict((n, (lo, hi)) for n, lo, hi in _module_symbols(src))
    assert syms["A"][0] == 1
    assert syms["f"] == (3, 4)
    assert syms["C"][0] == 6 and syms["C"][1] >= 8
    assert "m" not in syms  # nested, not module level


def test_module_symbols_survives_a_syntax_error() -> None:
    assert _module_symbols("def (:\n") == []


def _scope_lane(tmp_path: Path, path: str, source: str):
    repo = make_repo(tmp_path / "repo", verify_body="true")
    lane = tmp_path / "lane"
    subprocess.run(["git", "worktree", "add", "-b", "zecor/0001", str(lane), "HEAD"],
                   cwd=repo, check=True, capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=lane,
                          capture_output=True, text=True).stdout.strip()
    (lane / path).parent.mkdir(parents=True, exist_ok=True)
    (lane / path).write_text(source)
    subprocess.run(["git", "add", "-A"], cwd=lane, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=lane, check=True, capture_output=True)
    diff = subprocess.run(["git", "diff", f"{base}..HEAD"], cwd=lane,
                          capture_output=True, text=True).stdout
    return lane, diff


def test_symbol_scope_quiet_under_the_ceiling(tmp_path: Path) -> None:
    src = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(3))
    lane, diff = _scope_lane(tmp_path, "app/m.py", src)
    assert symbol_scope_findings(card(files=["app/m.py"]), diff, lane) == []


def test_symbol_scope_flags_a_sprawling_diff(tmp_path: Path) -> None:
    src = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(6))
    lane, diff = _scope_lane(tmp_path, "app/m.py", src)
    out = symbol_scope_findings(card(files=["app/m.py"]), diff, lane)
    assert len(out) == 1 and out[0].lens == "scope" and out[0].file == "app/m.py"
    assert "6 module-level symbols" in out[0].summary


def test_symbol_scope_skips_refactor_intent(tmp_path: Path) -> None:
    src = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(6))
    lane, diff = _scope_lane(tmp_path, "app/m.py", src)
    c = Card("0001", "demo", "refactor the helpers into one place",
             "./.fleet/verify.sh", ["app/m.py"])
    assert symbol_scope_findings(c, diff, lane) == []


def test_complexity_finding_flags_a_gnarly_function(tmp_path: Path) -> None:
    from zecor import _rustbridge

    if _rustbridge.binary("zecor-astscan") is None:
        pytest.skip("zecor-astscan not built")
    body = "\n".join(f"        if x == {i}:\n            return {i}" for i in range(30))
    src = f"def huge(x):\n{body}\n    return -1\n\ndef small():\n    return 0\n"
    lane, diff = _scope_lane(tmp_path, "app/m.py", src)
    out = selfreview.complexity_findings(diff, lane)
    assert len(out) == 1
    assert out[0].lens == "complexity" and "huge" in out[0].summary
    assert out[0].file == "app/m.py"


def test_complexity_finding_quiet_on_ordinary_code(tmp_path: Path) -> None:
    from zecor import _rustbridge

    if _rustbridge.binary("zecor-astscan") is None:
        pytest.skip("zecor-astscan not built")
    src = "def ok(a, b):\n    if a:\n        return b\n    return a\n"
    lane, diff = _scope_lane(tmp_path, "app/m.py", src)
    assert selfreview.complexity_findings(diff, lane) == []


def test_symbol_scope_skips_unbounded_cards(tmp_path: Path) -> None:
    src = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(6))
    lane, diff = _scope_lane(tmp_path, "app/m.py", src)
    c = Card("0001", "demo", "do a big thing", "./.fleet/verify.sh",
             ["app/m.py"], klass=Klass.UNBOUNDED)
    assert symbol_scope_findings(c, diff, lane) == []


def test_symbol_scope_ignores_non_python_and_tests(tmp_path: Path) -> None:
    lane, diff = _scope_lane(tmp_path, "notes.md", "\n".join(f"# heading {i}" for i in range(40)))
    assert symbol_scope_findings(card(files=["notes.md"]), diff, lane) == []