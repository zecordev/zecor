# SPDX-License-Identifier: Apache-2.0
"""The review step."""

from __future__ import annotations

import pytest

from zecor import review
from zecor.cards import Card
from zecor.review import Finding, ReviewResult, load_lens, pick_lenses, review_branch


def card() -> Card:
    return Card(
        "0001", "demo", "add a health endpoint", "./.fleet/verify.sh", ["src/api/health.ts"]
    )


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (["docs/guide.md"], ["prose"]),
        (["src/router/resolve.test.ts"], ["tests"]),
        (["package-lock.json"], ["security", "performance"]),
        (["src/components/Badge.tsx"], ["accessibility", "correctness", "tests"]),
        (["src/api/users.ts"], ["security", "correctness", "tests"]),
        (["Dockerfile"], ["tests", "performance", "correctness"]),
        (["src/util/math.ts"], ["correctness", "tests"]),
    ],
)
def test_pick_lenses_by_diff_shape(files: list[str], expected: list[str]) -> None:
    assert pick_lenses(files) == expected


def test_load_lens_strips_frontmatter() -> None:
    body = load_lens("correctness")
    assert not body.startswith("---")
    assert "name: correctness" not in body
    assert len(body) > 100  # got the real lens body


def test_review_skips_on_empty_diff() -> None:
    r = review_branch(card(), "", ledger_has_frontier=True)
    assert r.verdict == "no-review"
    assert r.skipped == "empty diff"


DIFF = """diff --git a/src/api/health.ts b/src/api/health.ts
--- a/src/api/health.ts
+++ b/src/api/health.ts
@@ -0,0 +1,3 @@
+export function health() {
+  return { ok: true };
+}
"""


def test_review_local_only_when_no_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review, "available", lambda: True)
    monkeypatch.setattr(review, "read_tier", lambda: "solo")
    monkeypatch.setattr(
        review,
        "_run_lens_local",
        lambda name, diff, c: [Finding(name, "low", "src/api/health.ts", 2, "x")],
    )
    monkeypatch.setattr(review, "_verify_local", lambda f, d: "CONFIRMED")
    r = review_branch(card(), DIFF, ledger_has_frontier=False)
    assert r.verdict == "local-only"
    assert "every frontier tier dry" in r.skipped
    assert r.findings


def test_review_runs_frontier_at_pro_and_takes_its_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review, "available", lambda: True)
    monkeypatch.setattr(review, "read_tier", lambda: "pro")
    monkeypatch.setattr(review, "_run_lens_local", lambda name, diff, c: [])
    monkeypatch.setattr(
        review,
        "ask",
        lambda prompt, **kw: (
            '{"verdict":"needs-work","notes":"add a test","extra_findings":'
            '[{"severity":"medium","file":"src/api/health.ts","line":1,"summary":"no test"}]}',
            "t4",
        ),
    )
    r = review_branch(card(), DIFF, ledger_has_frontier=True)
    assert r.verdict == "needs-work"
    assert r.tier_used == "t4"  # the real tier ask() reported -- so the ledger can record it
    assert any(f.lens == "frontier" for f in r.findings)


def test_refuted_local_findings_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review, "available", lambda: True)
    monkeypatch.setattr(review, "read_tier", lambda: "solo")
    monkeypatch.setattr(
        review, "_run_lens_local", lambda name, diff, c: [Finding(name, "high", "f", 1, "bogus")]
    )
    monkeypatch.setattr(review, "_verify_local", lambda f, d: "REFUTED")
    r = review_branch(card(), DIFF, ledger_has_frontier=False)
    assert r.findings == []


def test_summary_line_and_markdown() -> None:
    r = ReviewResult(
        verdict="ship",
        lenses_run=["tests"],
        findings=[Finding("tests", "high", "a.ts", 3, "no assertion", "CONFIRMED")],
        tier_used="frontier",
    )
    assert "ship" in r.summary_line()
    assert "1 high" in r.summary_line()
    md = r.as_markdown()
    assert "**high**" in md and "a.ts:3" in md