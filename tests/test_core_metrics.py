# SPDX-License-Identifier: Apache-2.0
"""core.metrics: read-only ledger stats, correct-when-empty."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zecor import config
from zecor.core import metrics


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "db" / "zecor.db"
    p.parent.mkdir(parents=True)
    monkeypatch.setattr(config, "DB_PATH", p)
    return p


def _seed(rows: list[tuple[str, str, str, str, str, int, int]]) -> None:
    db = sqlite3.connect(config.DB_PATH)
    db.execute(
        "CREATE TABLE attempt (id INTEGER PRIMARY KEY, task_id TEXT, tier TEXT, vendor TEXT, "
        "model TEXT, started_at TEXT, ended_at TEXT, in_tokens INT, out_tokens INT, "
        "verify_exit INT, outcome TEXT, reason TEXT)"
    )
    db.executemany(
        "INSERT INTO attempt (tier, model, started_at, ended_at, outcome, in_tokens, out_tokens) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()
    db.close()


def test_empty_ledger_is_a_clean_zero() -> None:
    s = metrics.summary()
    assert s.empty and s.total_attempts == 0 and s.by_tier == []
    assert s.median_cycle_s is None and s.est_cost_usd == 0.0
    assert s.daily_lands and set(s.daily_lands) == {0}


def test_no_db_file_at_all() -> None:
    config.DB_PATH.unlink(missing_ok=True)
    assert metrics.summary().empty
    assert metrics.landrate_by_tier() == []


def test_landrate_by_tier() -> None:
    _seed([
        ("t1", "qwen", "2026-08-30T01:00:00+00:00", "2026-08-30T01:02:00+00:00", "pass", 0, 0),
        ("t1", "qwen", "2026-08-30T01:05:00+00:00", "2026-08-30T01:06:00+00:00", "fail", 0, 0),
        ("t3", "codex", "2026-08-30T02:00:00+00:00", "2026-08-30T02:10:00+00:00", "pass", 0, 0),
    ])
    by = {t.tier: t for t in metrics.landrate_by_tier()}
    assert by["t1"].attempts == 2 and by["t1"].lands == 1 and by["t1"].rate == 0.5
    assert by["t3"].rate == 1.0


def test_cycle_time_and_cost() -> None:
    _seed([
        ("t4", "claude-opus", "2026-08-30T01:00:00+00:00", "2026-08-30T01:01:00+00:00",
         "pass", 1_000_000, 200_000),
        ("t4", "claude-opus", "2026-08-30T02:00:00+00:00", "2026-08-30T02:05:00+00:00",
         "pass", 0, 0),
    ])
    s = metrics.summary()
    assert s.median_cycle_s == 180.0  # median of 60s and 300s
    # 1M in * $15 + 0.2M out * $75 = 15 + 15 = $30
    assert s.est_cost_usd == 30.0


def test_daily_lands_series_len_matches_window() -> None:
    assert len(metrics.daily_lands(days=7)) == 7


def test_sparkline_shapes() -> None:
    assert metrics.sparkline([]) == ""
    assert metrics.sparkline([0, 0, 0]) == "▁▁▁"
    s = metrics.sparkline([0, 1, 2, 3, 4, 5, 6, 7])
    assert s[0] == "▁" and s[-1] == "█" and len(s) == 8