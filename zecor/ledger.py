# SPDX-License-Identifier: Apache-2.0
"""The quota ledger: learn vendor caps rather than hardcode them. Start with cap = NULL
and a conservative pace; when a vendor returns a rate-limit error, record `used` at that
moment as the observed cap and the reset timestamp -- within a few nights this knows the
real limits better than any vendor doc, and stays correct when the plans change.

T2b (Antigravity) weekly is a HARD cap on a fixed-reset calendar, not a rolling window.
T3 (Codex) 5h bucket is SHARED with interactive CLI/web/IDE use, so `used` here is a
lower bound -- treat T3 estimates as optimistic.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zecor.tiers import VENDOR_OF, Tier

# nightly pace before a cap is known -- deliberately timid
CONSERVATIVE_ATTEMPTS_BEFORE_LEARNED = 6

# fraction of a *learned* cap left untouched as reserve -- draining the Pro pool
# overnight leaves you throttled at 09:00
RESERVE_FRACTION = 0.4

# T4 (Claude Pro) resets on a 5-hour rolling window shared with your daytime use; cap
# Zecor at a few calls per window so a busy night can't drain it. Override with
# ZECOR_T4_CALLS_PER_WINDOW.
T4_WINDOW_HOURS = 5
T4_CALLS_PER_WINDOW = int(os.environ.get("ZECOR_T4_CALLS_PER_WINDOW", "2"))


class Ledger:
    def __init__(self, db_path: Path) -> None:
        # check_same_thread=False + WAL so each concurrent lane can hold its own
        # connection to the same file
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=15.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout = 15000")
        self.db.execute("PRAGMA foreign_keys = ON")
        with contextlib.suppress(sqlite3.OperationalError):
            self.db.execute("PRAGMA journal_mode = WAL")  # persistent; first opener sets it
        # every schema statement is CREATE ... IF NOT EXISTS, so applying it here too
        # (not only in `open()`) means a bare `Ledger(path)` on a db another module
        # created still has its tables, at negligible cost
        from zecor.config import SCHEMA_PATH

        if SCHEMA_PATH.exists():
            with contextlib.suppress(sqlite3.OperationalError):
                self.db.executescript(SCHEMA_PATH.read_text())
                self.db.commit()
        # one quota row per (vendor, window) -- stops two concurrent lanes racing in
        # duplicate rows
        with contextlib.suppress(sqlite3.OperationalError):
            self.db.execute(
                "DELETE FROM quota WHERE rowid NOT IN "
                "(SELECT MAX(rowid) FROM quota GROUP BY vendor, window)"
            )
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS quota_vendor_window "
                "ON quota (vendor, window)"
            )
            self.db.commit()

    @classmethod
    def open(cls, db_path: Path, schema_path: Path) -> Ledger:
        """Open the ledger. Schema is applied in __init__ (idempotent); `schema_path` is
        kept in the signature for call-site clarity."""
        _ = schema_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(db_path)

    # -- writes -----------------------------------------------------------------

    def record_attempt(
        self,
        *,
        task_id: str,
        tier: Tier,
        model: str | None,
        in_tokens: int,
        out_tokens: int,
        verify_exit: int,
        outcome: str,  # pass | fail | timeout | refused | ratelimited
        reason: str,
    ) -> None:
        now = _now_iso()
        self.db.execute(
            "INSERT INTO attempt (task_id, tier, vendor, model, started_at, ended_at, "
            "in_tokens, out_tokens, verify_exit, outcome, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                tier.value,
                VENDOR_OF.get(tier, "local"),
                model,
                now,
                now,
                in_tokens,
                out_tokens,
                verify_exit,
                outcome,
                reason,
            ),
        )
        self.db.commit()

    # an unparseable reset time must keep the tier dry a bounded interval then genuinely
    # expire -- not fail open onto a pool just throttled, not wedge forever
    UNPARSEABLE_RESET_HOLD_H = 5.0

    def mark_dry(self, tier: Tier, *, window: str, hours: float) -> None:
        """A worker came back refused/ratelimited/timeout with no RateLimit of its own --
        record a hold so route() skips this tier until `hours` from now, without
        touching a learned `cap`."""
        resets_at = (datetime.now(UTC) + timedelta(hours=hours)).isoformat()
        self._upsert_quota(VENDOR_OF[tier], window, resets_at, used_now=None)

    def observe_ratelimit(self, tier: Tier, *, window: str, used_now: int, resets_at: str) -> None:
        """A rate-limit error just came back. The tier is dry until `resets_at`; an
        unparseable timestamp becomes a bounded conservative hold."""
        if _parse_iso(resets_at) is None:
            resets_at = (
                datetime.now(UTC) + timedelta(hours=self.UNPARSEABLE_RESET_HOLD_H)
            ).isoformat()
        self._upsert_quota(VENDOR_OF[tier], window, resets_at, used_now=used_now)

    def _upsert_quota(
        self, vendor: str, window: str, resets_at: str, *, used_now: int | None
    ) -> None:
        """One row per (vendor, window). `used_now=None` leaves a learned `used`/`cap`
        alone (a hold); an int records it as the observed cap."""
        try:
            if used_now is None:
                self.db.execute(
                    "INSERT INTO quota (vendor, window, resets_at, used, cap) "
                    "VALUES (?,?,?,0,NULL) "
                    "ON CONFLICT(vendor, window) DO UPDATE SET resets_at = excluded.resets_at",
                    (vendor, window, resets_at),
                )
            else:
                self.db.execute(
                    "INSERT INTO quota (vendor, window, resets_at, used, cap) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(vendor, window) DO UPDATE SET "
                    "resets_at = excluded.resets_at, used = excluded.used, cap = excluded.cap",
                    (vendor, window, resets_at, used_now, used_now),
                )
        except sqlite3.OperationalError:
            # legacy DB with no UNIQUE(vendor, window) index -- manual upsert
            cur = self.db.execute(
                "UPDATE quota SET resets_at = ? WHERE vendor = ? AND window = ?",
                (resets_at, vendor, window),
            )
            if cur.rowcount == 0:
                self.db.execute(
                    "INSERT INTO quota (vendor, window, resets_at, used, cap) VALUES (?,?,?,?,?)",
                    (vendor, window, resets_at, used_now or 0, used_now),
                )
            elif used_now is not None:
                self.db.execute(
                    "UPDATE quota SET used = ?, cap = ? WHERE vendor = ? AND window = ?",
                    (used_now, used_now, vendor, window),
                )
        self.db.commit()

    def note_deadend(self, fingerprint: str, approach: str, note: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO deadend (fingerprint, approach, note) VALUES (?,?,?)",
            (fingerprint, approach, note),
        )
        self.db.commit()

    def clear_deadend(self, fingerprint: str) -> bool:
        """Drop a negative-cache entry so `zecor retry` can re-run the task. Returns
        whether a row was removed."""
        cur = self.db.execute("DELETE FROM deadend WHERE fingerprint = ?", (fingerprint,))
        self.db.commit()
        return cur.rowcount > 0

    # -- reads ----------------------------------------------------------------

    def is_deadend(self, fingerprint: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM deadend WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            is not None
        )

    def _t4_calls_this_window(self) -> int:
        since = (datetime.now(UTC) - timedelta(hours=T4_WINDOW_HOURS)).isoformat()
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM attempt WHERE tier = 't4' AND started_at >= ?",
            (since,),
        ).fetchone()
        return int(row["n"])

    def has(self, tier: Tier) -> bool:
        """Headroom right now? T0/T1 always; a vendor with an unexpired quota row is
        dry, otherwise a timid pre-learned pace applies."""
        if tier in (Tier.T0, Tier.T1):
            return True
        if tier is Tier.T4 and self._t4_calls_this_window() >= T4_CALLS_PER_WINDOW:
            return False  # protect the Pro pool for daytime use
        vendor = VENDOR_OF.get(tier)
        if vendor is None:
            return False
        rows = self.db.execute("SELECT resets_at FROM quota WHERE vendor = ?", (vendor,)).fetchall()
        if not rows:
            return self._attempts_this_window(vendor) < CONSERVATIVE_ATTEMPTS_BEFORE_LEARNED
        now = datetime.now(UTC)
        for r in rows:
            resets = _parse_iso(r["resets_at"])
            if resets is None or now >= resets:
                continue  # unknown/past reset -> treat this row as expired
            return False  # a live rate-limit row -> the tier is dry
        return True

    def route_stats(self) -> dict[tuple[str, str | None], tuple[int, int]]:
        """(tier, model) -> (trials, wins) over every recorded attempt. Feeds
        ZECOR_EV_ROUTING; empty until there is history."""
        out: dict[tuple[str, str | None], tuple[int, int]] = {}
        try:
            rows = self.db.execute("SELECT tier, model, outcome FROM attempt").fetchall()
        except sqlite3.Error:
            return {}
        for r in rows:
            key = (str(r["tier"]), r["model"])
            trials, wins = out.get(key, (0, 0))
            out[key] = (trials + 1, wins + (1 if r["outcome"] == "pass" else 0))
        return out

    def month_spend_usd(self) -> float:
        start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        row = self.db.execute(
            "SELECT COALESCE(SUM(in_tokens) * 0.20 + SUM(out_tokens) * 1.20, 0) / 1e6 AS usd "
            "FROM attempt WHERE vendor = 'openai-metered' AND started_at >= ?",
            (start.isoformat(),),
        ).fetchone()
        return float(row["usd"])

    def _attempts_this_window(self, vendor: str) -> int:
        # coarse: attempts against this vendor today, the pre-learned pace guard
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM attempt WHERE vendor = ? AND started_at >= ?",
            (vendor, start.isoformat()),
        ).fetchone()
        return int(row["n"])

    def close(self) -> None:
        self.db.close()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)