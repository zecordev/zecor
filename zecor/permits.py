# SPDX-License-Identifier: Apache-2.0
"""Cross-process permits for local-model work. `resources.can_start()` is advice only --
two processes can both read "ok" and start an aider, and `pgrep` can't see a `zecor run`
doing briefs over HTTP. This is the enforcement: a flock-guarded JSON registry under
`ZECOR_ROOT/.permits/`, capped at `ZECOR_LOCAL_MODEL_MAX`; LIGHT kinds never need one.

Usage:

    with acquire("local_lane", timeout_s=600) as p:
        if p is None:
            ...  # no slot inside the window -> DEFER
        else:
            run_aider(...)
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

from zecor.config import (
    MODEL_PRIMARY_CODER,
    MODEL_REASONER,
    MODEL_ROUTER,
    ZECOR_ROOT,
)
from zecor.resources import (
    BIG_MODEL_GB,
    KV_HEADROOM_GB,
    LIGHT_KINDS,
    LOCAL_KINDS,
    LOCAL_MODEL_MAX,
    RAM_FLOOR_GB,
    snapshot,
)

# Which resident model each local `kind` drives -- so acquire() can size its footprint.
_KIND_MODEL = {
    "local_lane": MODEL_PRIMARY_CODER,
    "aider": MODEL_PRIMARY_CODER,
    "local_review": MODEL_REASONER,
    "local_model": MODEL_ROUTER,
}
_DEFAULT_GB = {  # fallback when the model isn't resident / lms is unreadable
    "local_lane": 18.0,
    "aider": 18.0,
    "local_review": 12.0,
    "local_model": 3.0,
}

PERMIT_DIR = ZECOR_ROOT / ".permits"
_REGISTRY = PERMIT_DIR / "local.json"
_LOCK = PERMIT_DIR / "local.lock"
# Backstop only -- the reliable liveness check is the pid start-time token; anything
# past 2h (a permit wraps one ~900s worker call) is leaked.
_STALE_S = 7200.0


def _key() -> str:
    return f"{os.getpid()}:{threading.get_ident()}"


def _proc_token(pid: int) -> str:
    """Cheap pid identity that changes on reuse: its start time from `ps`. Empty if
    `ps` can't say -- callers then lean on the age backstop."""
    try:
        return subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3.0, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _pid_of(key: str) -> int:
    try:
        return int(key.split(":", 1)[0])
    except (ValueError, IndexError):
        return -1


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours
    return True


@contextlib.contextmanager
def _flock_ctx() -> Iterator[None]:
    """Hold an exclusive flock on the registry lock file for the block. On a platform
    without fcntl this degrades to no locking -- acceptable, that platform is not a
    target and the JSON writes are still atomic via os.replace."""
    PERMIT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-posix
        yield
        return
    fh = open(_LOCK, "w")  # noqa: SIM115
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fh.close()


def _load() -> dict[str, dict[str, object]]:
    try:
        data = json.loads(_REGISTRY.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time()
    out: dict[str, dict[str, object]] = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        pid = _pid_of(k)
        if not _alive(pid):
            continue
        tok = str(v.get("tok", ""))
        if tok and _proc_token(pid) != tok:
            continue  # pid was recycled to a different process -- the holder is gone
        if now - float(v.get("ts", 0)) > _STALE_S:
            continue  # backstop: live pid, no usable token, ancient -- assume leaked
        out[k] = v
    return out


def _save(data: dict[str, dict[str, object]]) -> None:
    PERMIT_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(PERMIT_DIR), prefix="local.", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _REGISTRY)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@dataclass
class Permit:
    kind: str
    key: str
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        with _flock_ctx():
            data = _load()
            data.pop(self.key, None)
            _save(data)
        self._released = True  # only after the entry is actually gone

    def __enter__(self) -> Permit:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class _NoPermit:
    """LIGHT work -- nothing to hold, but a real context manager so
    `with acquire(...) as p` always works and `p is None` still reads correctly."""

    def release(self) -> None:
        return

    def __bool__(self) -> bool:
        return True

    def __enter__(self) -> _NoPermit:
        return self

    def __exit__(self, *exc: object) -> None:
        return


def held() -> list[dict[str, object]]:
    """Live permits right now (for `zecor resources` and can_start)."""
    with _flock_ctx():
        return [{"key": k, **v} for k, v in _load().items()]


def _as_gb(v: object, default: float = 12.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _model_gb(kind: str, sizes: dict[str, float]) -> float:
    """Resident footprint of the model this kind uses. Falls back to a conservative
    default when the model isn't loaded / lms is unreadable."""
    model = _KIND_MODEL.get(kind, MODEL_ROUTER)
    if model in sizes and sizes[model] > 0:
        return sizes[model]
    # lms sometimes keys by a variant suffix -- match a prefix
    for k, v in sizes.items():
        if v > 0 and (k.startswith(model) or model.startswith(k)):
            return v
    return _DEFAULT_GB.get(kind, 12.0)


def _grant_ok(mykey: str, kind: str, data: dict[str, dict[str, object]],
              snap: object) -> bool:
    """Model-size-aware admission. One consumer for a big model (>= BIG_MODEL_GB);
    small models may double up within a RAM budget, capped by LOCAL_MODEL_MAX."""
    if mykey in data:
        return True
    if len(data) >= LOCAL_MODEL_MAX:
        return False
    sizes: dict[str, float] = getattr(snap, "lmstudio_sizes", {}) or {}
    my_gb = _model_gb(kind, sizes)
    held_gb = [_as_gb(v.get("gb")) for v in data.values()]
    if my_gb >= BIG_MODEL_GB:
        return not any(g >= BIG_MODEL_GB for g in held_gb)  # one big model at a time
    # small model: fit within the reclaimable-RAM budget above the floor
    free = float(getattr(snap, "ram_free_gb", 0.0)) - RAM_FLOOR_GB
    used = sum(g + KV_HEADROOM_GB for g in held_gb)
    return (used + my_gb + KV_HEADROOM_GB) <= max(free, my_gb + KV_HEADROOM_GB)


def acquire(
    kind: str, *, timeout_s: float = 0.0, poll_s: float = 0.5
) -> Permit | _NoPermit | None:
    """Get a permit for `kind`. LIGHT kinds get a no-op permit immediately; LOCAL kinds
    block up to `timeout_s` for a model-size-aware slot. An unknown kind is treated as
    LOCAL (fail safe)."""
    if kind in LIGHT_KINDS:
        return _NoPermit()
    if kind not in LOCAL_KINDS:
        kind = "local_model"  # unknown -> gated, not waved through
    deadline = time.monotonic() + max(0.0, timeout_s)
    mykey = _key()
    mytok = _proc_token(os.getpid())
    snap = snapshot()
    last_snap = time.monotonic()
    while True:
        if time.monotonic() - last_snap > 10.0:  # refresh the machine read periodically
            snap = snapshot()
            last_snap = time.monotonic()
        with _flock_ctx():
            data = _load()
            if _grant_ok(mykey, kind, data, snap):
                data[mykey] = {
                    "kind": kind, "ts": time.time(), "tok": mytok,
                    "gb": _model_gb(kind, getattr(snap, "lmstudio_sizes", {}) or {}),
                }
                _save(data)
                return Permit(kind, mykey)
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_s)