# SPDX-License-Identifier: Apache-2.0
"""The quota ledger -- cap learning and the T4 window cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zecor import ledger as ledger_mod
from zecor.ledger import Ledger, _parse_iso
from zecor.tiers import Tier

SCHEMA = Path(__file__).resolve().parents[1] / "setup" / "schema.sql"


def _led(tmp_path: Path) -> Ledger:
    return Ledger.open(tmp_path / "zecor.db", SCHEMA)


def test_fresh_ledger_has_every_tier(tmp_path: Path) -> None:
    led = _led(tmp_path)
    for t in (Tier.T1, Tier.T2A, Tier.T3, Tier.T4, Tier.T5):
        assert led.has(t) is True


def test_t4_window_cap_blocks_after_n_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_mod, "T4_CALLS_PER_WINDOW", 2)
    led = _led(tmp_path)
    for _ in range(2):
        led.record_attempt(
            task_id="t",
            tier=Tier.T4,
            model="claude-sonnet-5",
            in_tokens=1,
            out_tokens=1,
            verify_exit=1,
            outcome="fail",
            reason="x",
        )
    assert led.has(Tier.T4) is False  # cap hit
    assert led.has(Tier.T3) is True  # other tiers unaffected


def test_t4_calls_outside_the_window_do_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_mod, "T4_CALLS_PER_WINDOW", 1)
    led = _led(tmp_path)
    old = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    led.db.execute(
        "INSERT INTO attempt (task_id, tier, vendor, started_at, outcome) VALUES (?,?,?,?,?)",
        ("t", "t4", "anthropic-pro", old, "fail"),
    )
    led.db.commit()
    assert led.has(Tier.T4) is True  # the old call aged out


def test_ratelimit_row_makes_the_tier_dry_until_it_resets(tmp_path: Path) -> None:
    # regression: has() compared an ISO `now` to "next-midnight-PT" lexically, so a
    # rate-limit row never expired AND (worse) a 429 *raised* the ceiling from 6 to ~900.
    led = _led(tmp_path)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    led.observe_ratelimit(Tier.T2A, window="daily", used_now=0, resets_at=future)
    assert led.has(Tier.T2A) is False  # dry until the window resets

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    led.observe_ratelimit(Tier.T2A, window="daily", used_now=0, resets_at=past)
    assert led.has(Tier.T2A) is True  # window reset -> available again


def test_unparseable_reset_timestamp_becomes_a_bounded_hold(tmp_path: Path) -> None:
    # A 429 with a reset time we cannot parse must NOT fail open (an unattended run
    # would hammer the pool it was just throttled on). It is stored as a conservative
    # fixed hold that genuinely expires -- not fail-open, not wedged forever
    led = _led(tmp_path)
    led.observe_ratelimit(Tier.T2A, window="daily", used_now=0, resets_at="next-midnight-PT")
    assert led.has(Tier.T2A) is False  # held
    stored = led.db.execute(
        "SELECT resets_at FROM quota WHERE vendor = 'google-free'"
    ).fetchone()["resets_at"]
    assert _parse_iso(stored) is not None  # the garbage was replaced with a real ISO time

    # and it does expire -- shove the stored time into the past
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    led.observe_ratelimit(Tier.T2A, window="daily", used_now=0, resets_at=past)
    assert led.has(Tier.T2A) is True


def test_mark_dry_skips_the_tier_for_the_given_hours(tmp_path: Path) -> None:
    led = _led(tmp_path)
    led.mark_dry(Tier.T3, window="rolling", hours=5)
    assert led.has(Tier.T3) is False
    led.mark_dry(Tier.T3, window="rolling", hours=-1)  # already elapsed
    assert led.has(Tier.T3) is True


def test_observe_ratelimit_keeps_one_row_per_vendor_window(tmp_path: Path) -> None:
    # regression: SELECT-then-INSERT/UPDATE was not atomic -- two concurrent lanes
    # could both miss the row and write duplicates. The upsert + UNIQUE index means
    # a second observation overwrites, never appends.
    led = _led(tmp_path)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    led.observe_ratelimit(Tier.T2A, window="daily", used_now=1, resets_at=past)
    led.observe_ratelimit(Tier.T2A, window="daily", used_now=9, resets_at=future)
    rows = led.db.execute(
        "SELECT used, resets_at FROM quota WHERE vendor = ? AND window = 'daily'",
        (led.db.execute("SELECT vendor FROM quota LIMIT 1").fetchone()["vendor"],),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["used"] == 9  # the later observation won
    assert led.has(Tier.T2A) is False  # and it is the live one


def test_negative_cache_round_trip(tmp_path: Path) -> None:
    led = _led(tmp_path)
    assert led.is_deadend("abc") is False
    led.note_deadend("abc", "aider", "kept reverting")
    assert led.is_deadend("abc") is True