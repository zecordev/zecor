# SPDX-License-Identifier: Apache-2.0
"""Run a lane's test command under `zecor-sandbox` (OS-level isolation).

`run_capped` already strips credentials and pins `PATH` in hardened mode; this goes
further for the one command that runs attacker-influenced code -- the repo's own gate
-- confining it to writing inside the lane and denying the network via Landlock
(Linux) / sandbox-exec (macOS) / a Job Object (Windows). Only engaged when the Rust
helper is built and the mode wants isolation; otherwise the command runs unchanged.
"""

from __future__ import annotations

from pathlib import Path

from zecor import _rustbridge, modes


def wanted() -> bool:
    return modes.writes_are_isolated_required() and _rustbridge.binary("zecor-sandbox") is not None


def wrap(
    cmd: list[str],
    *,
    workdir: Path,
    allow_write: list[Path] | None = None,
    allow_net: bool = False,
    force: bool = False,
) -> list[str]:
    """Prefix `cmd` with `zecor-sandbox run --policy ... --`; returns `cmd` unchanged
    when the sandbox isn't engaged. `force` runs it under the sandbox regardless of
    mode, as long as the binary is built (for `zecor sandbox`)."""
    b = _rustbridge.binary("zecor-sandbox")
    if b is None or not (force or wanted()):
        return cmd
    writes = sorted({str(workdir), *(str(p) for p in (allow_write or []))})
    pol = workdir / ".zecor-sandbox.policy"
    try:
        pol.write_text(
            f"workdir = {workdir}\n"
            f"allow_write = {', '.join(writes)}\n"
            f"allow_net = {'true' if allow_net else 'false'}\n"
        )
    except OSError:
        return cmd
    return [b, "run", "--policy", str(pol), "--", *cmd]
