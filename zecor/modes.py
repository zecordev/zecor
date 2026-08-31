# SPDX-License-Identifier: Apache-2.0
"""Runtime security posture.

Three modes, set by `ZECOR_MODE` or the `mode` key in `zecor.config.json`:

  greenfield  Rapid prototyping, zero production users. Full autonomy: create files,
              branch, self-heal, open and merge PRs.
  guarded     Active feature work on an existing codebase. Autonomous branch + author +
              test; auto-merge blocked unless every deterministic check passes clean.
  hardened    Production / public repos / untrusted input. Read-only AST parsing, draft
              PRs only, no autonomous merge, allow-listed shell, external input treated
              as hostile.

`current_mode()` is the single source of truth; the gates below are the only sanctioned
way to ask "is this allowed here". Default, and the fallback for an unknown value, is
`hardened`.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from zecor import config


class Mode(StrEnum):
    GREENFIELD = "greenfield"
    GUARDED = "guarded"
    HARDENED = "hardened"


_DEFAULT = Mode.HARDENED
_CONFIG_NAMES = ("zecor.config.json", ".zecor/config.json")


def _from_config_file() -> str | None:
    for name in _CONFIG_NAMES:
        p = config.ZECOR_ROOT / name
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            m = data.get("mode")
            if isinstance(m, str):
                return m.strip().lower()
    return None


@lru_cache(maxsize=1)
def _resolve() -> Mode:
    raw = (os.environ.get("ZECOR_MODE") or _from_config_file() or "").strip().lower()
    try:
        return Mode(raw)
    except ValueError:
        return _DEFAULT


def current_mode() -> Mode:
    return _resolve()


def reset_cache() -> None:
    """Tests set the env / config file then call this."""
    _resolve.cache_clear()


# -- capability gates -------------------------------------------------------

def automerge_allowed() -> bool:
    """Only greenfield merges without a human. guarded requires a clean deterministic
    sweep (the caller still checks that); hardened never merges autonomously."""
    return current_mode() is Mode.GREENFIELD


def automerge_requires_clean_sweep() -> bool:
    return current_mode() is Mode.GUARDED


def draft_pr_only() -> bool:
    return current_mode() is Mode.HARDENED


def shell_is_allow_listed() -> bool:
    """hardened forbids open-ended shell; every command is checked against a whitelist
    (see `zecor.toolgate`)."""
    return current_mode() is Mode.HARDENED


def treat_external_input_as_hostile() -> bool:
    """Wrap GitHub issues / PR comments / fetched web data in an untrusted container
    (`zecor.untrusted.wrap`) before any model sees them. Always on outside greenfield."""
    return current_mode() is not Mode.GREENFIELD


def writes_are_isolated_required() -> bool:
    """greenfield and hardened both expect an isolated workspace / container; guarded
    works on a checkout but only on its own branch."""
    return current_mode() in (Mode.GREENFIELD, Mode.HARDENED)


def as_dict() -> dict[str, object]:
    m = current_mode()
    return {
        "mode": m.value,
        "automerge_allowed": automerge_allowed(),
        "automerge_requires_clean_sweep": automerge_requires_clean_sweep(),
        "draft_pr_only": draft_pr_only(),
        "shell_allow_listed": shell_is_allow_listed(),
        "external_input_hostile": treat_external_input_as_hostile(),
    }


def config_search_paths() -> list[Path]:
    return [config.ZECOR_ROOT / n for n in _CONFIG_NAMES]
