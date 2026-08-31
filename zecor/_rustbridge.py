# SPDX-License-Identifier: Apache-2.0
"""Locate and invoke the compiled Rust helper binaries.

Each call site keeps its pure-Python implementation as a fallback, so the engine runs
identically with or without the binaries present (see RUST-TOOLS.md). Set
`ZECOR_RUST_BIN_DIR` to override the search path; set `ZECOR_NO_RUST=1` to force the
Python path (used by the test suite for determinism).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from zecor import config

_NAMES = (
    "zecor-secretscan",
    "zecor-tokcount",
    "zecor-astscan",
    "zecor-gitengine",
    "zecor-sandbox",
)


@lru_cache(maxsize=len(_NAMES))
def binary(name: str) -> str | None:
    """Absolute path to a built Rust helper, or None. Checks, in order:
    `ZECOR_RUST_BIN_DIR`, `<repo>/bin`, a local `target/release` or `target/debug` (for a
    from-source checkout of a tool repo), then PATH."""
    if os.environ.get("ZECOR_NO_RUST"):
        return None
    exe = f"{name}.exe" if os.name == "nt" else name
    override = os.environ.get("ZECOR_RUST_BIN_DIR", "").strip()
    roots = [Path(override)] if override else []
    roots += [
        config.REPO_ROOT / "bin",  # downloaded prebuilts / `cargo install --root`
        config.REPO_ROOT / "target" / "release",
        config.REPO_ROOT / "target" / "debug",
    ]
    for r in roots:
        p = r / exe
        if p.is_file() and (os.name == "nt" or os.access(p, os.X_OK)):
            return str(p)
    return shutil.which(exe) or shutil.which(name)


def run_json(
    name: str, args: list[str], stdin: str = "", *, timeout_s: float = 30.0
) -> object | None:
    """Invoke a helper, parse its stdout as JSON. Returns None (never raises) if the
    binary is missing, times out, crashes, or emits non-JSON -- the caller then uses its
    Python fallback."""
    b = binary(name)
    if b is None:
        return None
    try:
        p = subprocess.run(
            [b, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode not in (0, 1):  # 0 = ok, 1 = "found something" (still valid output)
        return None
    try:
        parsed: object = json.loads(p.stdout or "null")
    except json.JSONDecodeError:
        return None
    return parsed


def available() -> dict[str, bool]:
    return {n: binary(n) is not None for n in _NAMES}
