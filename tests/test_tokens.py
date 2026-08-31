# SPDX-License-Identifier: Apache-2.0
"""zecor.tokens -- Rust-backed token counting with a heuristic fallback."""

from __future__ import annotations

import pytest

from zecor import _rustbridge, tokens


def test_heuristic_when_forced_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_NO_RUST", "1")
    _rustbridge.binary.cache_clear()
    n = tokens.count("the quick brown fox jumps over the lazy dog")
    assert n > 0 and not tokens.exact_available()
    _rustbridge.binary.cache_clear()


def test_exact_matches_a_known_count() -> None:
    if _rustbridge.binary("zecor-tokcount") is None:
        pytest.skip("zecor-tokcount not built")
    assert tokens.count("hello world") == 2  # cl100k_base
    assert tokens.count("") == 0


def test_trim_respects_the_budget() -> None:
    if _rustbridge.binary("zecor-tokcount") is None:
        pytest.skip("zecor-tokcount not built")
    text = "one two three four five six seven eight nine ten " * 20
    out = tokens.trim(text, 12)
    assert tokens.count(out) <= 12
    assert tokens.trim("short", 100) == "short"


def test_heuristic_trim_is_proportional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_NO_RUST", "1")
    _rustbridge.binary.cache_clear()
    text = "x " * 4000  # ~1333 heuristic tokens
    out = tokens.trim(text, 100)
    assert 0 < len(out) < len(text)
    _rustbridge.binary.cache_clear()


def test_price_resolves_by_prefix_and_override() -> None:
    assert tokens.price_per_mtok("gpt-4o-2024-08-06") == (2.50, 10.00)
    assert tokens.price_per_mtok("claude-sonnet-4-5") == (3.00, 15.00)
    assert tokens.price_per_mtok("who-knows") is None


def test_cost_math(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_NO_RUST", "1")  # exercise the Python path
    _rustbridge.binary.cache_clear()
    c = tokens.cost("gpt-4o", 1_000_000, 500_000)
    assert abs(float(c["in_usd"]) - 2.50) < 1e-9
    assert abs(float(c["total_usd"]) - 7.50) < 1e-9
    c2 = tokens.cost("mystery", 1_000_000, price=(1.0, 2.0))
    assert abs(float(c2["in_usd"]) - 1.0) < 1e-9
    _rustbridge.binary.cache_clear()


def test_diff_trim_drops_hunks_to_budget() -> None:
    if _rustbridge.binary("zecor-tokcount") is None:
        pytest.skip("zecor-tokcount not built")
    d = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
    for i in range(12):
        n = i * 10 + 1
        d += f"@@ -{n},2 +{n},3 @@\n ctx\n-was{i}\n+now{i}\n+add{i}\n"
    full = tokens.count(d)
    out = tokens.diff_trim(d, full // 2)
    assert tokens.count(out) <= full // 2
    assert "diff --git a/x.py b/x.py" in out
    assert tokens.diff_trim(d, full + 50) == d
