# SPDX-License-Identifier: Apache-2.0
"""Filesystem and process primitives shared across `core`. No zecor imports but
`config`, so any module can depend on this without a cycle.

`file_lock`: an exclusive `fcntl.flock` held for the body. Separate `open()` per call so
two threads in one process contend on distinct open file descriptions.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from zecor import config


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def zecor_bin() -> list[str]:
    """argv prefix that runs this same CLI: the `bin/zecor` launcher if present, else
    `python -m zecor`."""
    launcher = config.REPO_ROOT / "bin" / "zecor"
    return [str(launcher)] if launcher.exists() else [sys.executable, "-m", "zecor"]