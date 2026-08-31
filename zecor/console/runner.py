# SPDX-License-Identifier: Apache-2.0
"""Run a `zecor` subcommand and stream its output, for the console pane.

Shells out (`asyncio.create_subprocess_exec`) rather than calling the CLI's parser
in-process -- `argparse` would `sys.exit()` on a bad argument or `--help` and take down
the TUI, and a synchronous call would block the event loop. Long jobs (`zecor run`) are
NOT run here -- the app routes those to `core.intents.run()` so they detach and outlive
the console.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from zecor import config
from zecor.core.locks import zecor_bin

# Subcommands that must be started detached, not streamed into the pane.
DETACHED = frozenset({"run"})


def split(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


@dataclass(frozen=True)
class RunOpts:
    max_n: int | None = None
    until: str | None = None


def run_opts(args: list[str]) -> RunOpts:
    """Pull --until HH:MM / --max N out of a `zecor run ...` typed in the console pane,
    so the detached run keeps its bounds instead of going unbounded."""
    max_n: int | None = None
    until: str | None = None
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a in ("--until", "-u") and nxt:
            until = nxt
        elif a in ("--max", "-n") and nxt.isdigit():
            max_n = int(nxt)
    return RunOpts(max_n, until)


async def stream(argv: list[str], on_line: Callable[[str], None]) -> int:
    """Run `zecor <argv>`, calling `on_line` for each output line. Returns the exit code
    (or -1 if the process could not start)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *zecor_bin(),
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(config.REPO_ROOT),
        )
    except OSError as e:
        on_line(f"! could not start: {e}")
        return -1
    if proc.stdout is None:
        on_line("! process opened with no stdout pipe")
        return -1
    while True:
        try:
            raw = await proc.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError):
            # a single line over the 64 KiB buffer with no newline -- drain a chunk and
            # keep going rather than letting it escape into the UI handler
            raw = await proc.stdout.read(65536)
            if raw:
                on_line(raw.decode(errors="replace").rstrip("\n"))
            continue
        if not raw:
            break
        on_line(raw.decode(errors="replace").rstrip("\n"))
    return await proc.wait()