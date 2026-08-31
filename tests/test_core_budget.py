# SPDX-License-Identifier: Apache-2.0
"""core.budget: the unattended spend ceiling."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zecor import config
from zecor.core import budget


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "db" / "zecor.db"
    p.parent.mkdir(parents=True)
    monkeypatch.setattr(config, "DB_PATH", p)
    for k in ("ZECOR_SPEND_DAILY_USD", "ZECOR_SPEND_WEEKLY_USD"):
        monkeypatch.delenv(k, raising=False)
    return p


def _schema() -> sqlite3.Connection:
    db = sqlite3.connect(config.DB_PATH)
    db.execute("CREATE TABLE quota (vendor TEXT, window TEXT, resets_at TEXT, used INT, cap INT)")
    db.execute(
        "CREATE TABLE attempt (id INTEGER PRIMARY KEY, task_id TEXT, tier TEXT, vendor TEXT, "
        "model TEXT, started_at TEXT, ended_at TEXT, in_tokens INT, out_tokens INT, "
        "verify_exit INT, outcome TEXT, reason TEXT)"
    )
    return db


def test_empty_is_ok() -> None:
    _schema().close()
    st = budget.ceiling_state()
    assert st.state == "ok" and st.may_spend_frontier and not st.should_pause


def test_no_db_is_ok() -> None:
    assert budget.ceiling_state().state == "ok"


def test_quota_at_80pct_throttles() -> None:
    db = _schema()
    db.execute("INSERT INTO quota VALUES ('anthropic','weekly','',85,100)")
    db.commit()
    db.close()
    st = budget.ceiling_state()
    assert st.state == "throttle" and not st.may_spend_frontier
    assert "anthropic weekly" in st.reason


def test_quota_exhausted_pauses() -> None:
    db = _schema()
    db.execute("INSERT INTO quota VALUES ('anthropic','5h','',100,100)")
    db.commit()
    db.close()
    assert budget.ceiling_state().should_pause


def test_usd_daily_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZECOR_SPEND_DAILY_USD", "10")
    db = _schema()
    now = datetime.now(UTC).isoformat()
    # 1M opus input = $15 -> over the $10 daily ceiling
    db.execute(
        "INSERT INTO attempt (model, started_at, in_tokens, out_tokens, outcome) "
        "VALUES ('claude-opus', ?, 1000000, 0, 'pass')", (now,),
    )
    db.commit()
    db.close()
    assert budget.ceiling_state().should_pause


def test_worst_signal_wins() -> None:
    db = _schema()
    db.execute("INSERT INTO quota VALUES ('anthropic','weekly','',85,100)")  # throttle
    db.execute("INSERT INTO quota VALUES ('openai','daily','',100,100)")  # pause
    db.commit()
    db.close()
    assert budget.ceiling_state().state == "pause"