# SPDX-License-Identifier: Apache-2.0
"""Keep Zecor alive and fed -- Endurance step 2 (minimal supervisor) + `watchd`.

`supervise_loop()` runs `zecor run` in bounded slices, and between slices replenishes
the queue from the roadmap, runs the always-on watchers, does the daily progress pass,
and honours STOP and the spend ceiling; a crashed slice backs off exponentially, a
clean one resets it. `watch_tick()` is the always-on part -- posting a question when a
card needs a human, notifying on a new zombie, narrating new attempts -- run both
inside the loop and standalone as `zecor watchd`.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zecor import config, notify
from zecor.core import (
    budget,
    progress,
    questions,
    replenish,
    transcript,
    view,
)
from zecor.core.locks import zecor_bin

HEARTBEAT = "supervise.heartbeat"
WATCH_STATE = "watch.state"
RUN_SLICE_HOURS = 6.0
POLL_S = 60.0
LOW_WATER = 2
BACKOFF_START_S = 60.0
BACKOFF_CAP_S = 1800.0
PROGRESS_EVERY_S = 86400.0


@dataclass
class _State:
    seen_zombies: list[str]
    last_attempt_id: int
    last_progress_at: float


def _state_path(name: str) -> Path:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return config.LOGS_DIR / name


def _load_state() -> _State:
    try:
        obj = json.loads(_state_path(WATCH_STATE).read_text())
        return _State(
            list(obj.get("seen_zombies", [])),
            int(obj.get("last_attempt_id", 0)),
            float(obj.get("last_progress_at", 0.0)),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _State([], 0, 0.0)


def _atomic_json(path: Path, obj: dict[str, object]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def _save_state(s: _State) -> None:
    _atomic_json(
        _state_path(WATCH_STATE),
        {
            "seen_zombies": s.seen_zombies[-200:],
            "last_attempt_id": s.last_attempt_id,
            "last_progress_at": s.last_progress_at,
        },
    )


def _write_heartbeat(**extra: object) -> None:
    _atomic_json(
        _state_path(HEARTBEAT),
        {"pid": os.getpid(), "at": datetime.now(UTC).isoformat(), **extra},
    )


def read_heartbeat() -> dict[str, object]:
    try:
        return dict(json.loads(_state_path(HEARTBEAT).read_text()))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _new_attempts(after_id: int) -> list[tuple[int, str, str, str, str, str]]:
    if not config.DB_PATH.exists():
        return []
    try:
        db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5.0)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, task_id, tier, model, outcome, reason FROM attempt "
            "WHERE id > ? ORDER BY id ASC LIMIT 200",
            (after_id,),
        ).fetchall()
        db.close()
    except sqlite3.Error:
        return []
    return [
        (int(r["id"]), str(r["task_id"] or "?"), str(r["tier"] or "?"),
         str(r["model"] or "-"), str(r["outcome"] or "?"), str(r["reason"] or ""))
        for r in rows
    ]


def watch_tick(state: _State | None = None) -> _State:
    """Post questions, notify new zombies, narrate new attempts. Idempotent."""

    s = state or _load_state()
    try:
        cards = view.cards()
    except Exception:
        return s

    with contextlib.suppress(Exception):
        questions.scan(cards)

    live_zombies = {c.id for c in cards if c.zombie}
    for zid in sorted(live_zombies - set(s.seen_zombies)):
        with _quiet():
            notify.notify(f"zombie {zid}", "its dispatch lease outlived the run", level="warn",
                          card_id=zid)
    s.seen_zombies = sorted(live_zombies)

    for aid, tid, tier, model, outcome, reason in _new_attempts(s.last_attempt_id):
        line = f"{tid} -> {tier}" + (f" ({model})" if model not in ("-", "None") else "")
        line += f": {outcome}" + (f" -- {reason[:100]}" if reason else "")
        try:
            transcript.append("zecor", "narration", line, card_id=tid, meta={"attempt": aid})
        except Exception:
            break  # append failed -- leave the cursor so this attempt is retried next tick
        s.last_attempt_id = max(s.last_attempt_id, aid)

    _save_state(s)
    return s


class _quiet:
    """Swallow ordinary exceptions from a watcher block -- but never KeyboardInterrupt /
    SystemExit, so Ctrl-C still stops the supervisor."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_rest: object) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)


def _ready_count() -> int:
    try:
        return sum(1 for c in view.cards() if c.state == "ready")
    except Exception:
        return 99  # unknown -> don't replenish


def _run_slice(hours: float, *, throttled: bool = False) -> int:
    """Run one bounded `zecor run` slice, its output appended to a log. Returns 1 on
    timeout or spawn failure (a crashed slice, from the loop's view). Under a budget
    throttle the slice is short and capped at a few cards."""
    until = (datetime.now().astimezone() + timedelta(hours=hours)).strftime("%H:%M")
    argv = [*zecor_bin(), "run", "--until", until]
    if throttled:
        argv += ["--max", "3"]
    log = config.LOGS_DIR / "supervise-run.log"
    try:
        with open(log, "a") as fh:
            proc = subprocess.run(
                argv, stdout=fh, stderr=subprocess.STDOUT, cwd=str(config.REPO_ROOT),
                timeout=hours * 3600 + 900, check=False,
            )
        return proc.returncode
    except (subprocess.TimeoutExpired, OSError):
        return 1


def _bump(backoff: float) -> float:
    return min(BACKOFF_CAP_S, backoff * 2 if backoff else BACKOFF_START_S)


def supervise_loop(
    *,
    run_hours: float = RUN_SLICE_HOURS,
    poll_s: float = POLL_S,
    low_water: int = LOW_WATER,
    once: bool = False,
    max_iters: int | None = None,
    run_slice: Callable[[float], int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """The keep-alive loop. `once=True` or `max_iters` bounds it; `run_slice` / `sleep`
    are injectable so a test can drive it without a real subprocess."""

    throttle_ref = [False]

    def _default_slice(h: float) -> int:
        return _run_slice(h, throttled=throttle_ref[0])

    do_slice = run_slice or _default_slice
    backoff = 0.0
    state = _load_state()
    iters = 0
    paused_notified = False

    def _done() -> bool:
        return once or bool(max_iters and iters >= max_iters)

    while True:
        iters += 1
        ceiling = budget.ceiling_state()
        _write_heartbeat(
            backoff_s=backoff, ceiling=ceiling.state, iters=iters,
            stopped=config.stop_requested(),
        )

        if config.stop_requested():
            if _done():
                return 0
            sleep(poll_s)
            continue

        if ceiling.should_pause:
            if not paused_notified:
                with _quiet():
                    notify.notify("zecor paused -- spend ceiling", ceiling.reason, level="crit")
                    config.STOP_FILE.write_text(f"spend ceiling: {ceiling.reason}\n")
                paused_notified = True
            if _done():
                return 0
            sleep(poll_s)
            continue
        paused_notified = False
        throttle_ref[0] = ceiling.state == "throttle"

        with _quiet():
            state = watch_tick(state)

        if _ready_count() < low_water:
            try:
                replenish.replenish_once(low_water=low_water)
            except replenish.ReplenishError:
                backoff = _bump(backoff)

        rc = do_slice(run_hours)
        backoff = 0.0 if rc == 0 else _bump(backoff)

        if time.time() - state.last_progress_at >= PROGRESS_EVERY_S:
            with contextlib.suppress(Exception):  # a failed pass retries next tick
                progress.run_once()
                state.last_progress_at = time.time()
                _save_state(state)

        if _done():
            return 0
        sleep(backoff or poll_s)