# SPDX-License-Identifier: Apache-2.0
"""Paths and constants.

Everything derives from the repo root so the tree stays movable.
"""

from __future__ import annotations

import os
from pathlib import Path

# Back-compat: honour any legacy `FLEET_*` env var when the `ZECOR_*` one is unset, so
# an existing deployment keeps working across the rename. Remove after a deprecation
# window.
for _k in [k for k in os.environ if k.startswith("FLEET_")]:
    _z = "ZECOR_" + _k[len("FLEET_") :]
    os.environ.setdefault(_z, os.environ[_k])

# The code checkout -- fixed, holds schema.sql and the prompt templates.
REPO_ROOT = Path(__file__).resolve().parents[1]
# The state tree -- tasks, lanes, db, digest, queue. Overridable so a second account
# (fleetbot) or a test can run against its own state without a second checkout.
ZECOR_ROOT = Path(os.environ.get("ZECOR_ROOT", REPO_ROOT))

DB_PATH = ZECOR_ROOT / "db" / "zecor.db"
SCHEMA_PATH = REPO_ROOT / "setup" / "schema.sql"
QUEUE_INBOX = ZECOR_ROOT / "queue" / "inbox.md"
TASKS_DIR = ZECOR_ROOT / "tasks"
PROJECTS_DIR = ZECOR_ROOT / "projects"  # drop requirements docs here for `zecor decompose`
LANES_DIR = ZECOR_ROOT / "lanes"
DIGEST_DIR = ZECOR_ROOT / "digest"
LOGS_DIR = ZECOR_ROOT / "logs"
PROMPTS_DIR = REPO_ROOT / "prompts"
RULES_DIR = ZECOR_ROOT / "rules"
STOP_FILE = ZECOR_ROOT / "STOP"  # touch to halt between tasks

# Local inference -- LM Studio's OpenAI-compatible endpoint.
LM_BASE_URL = os.environ.get("ZECOR_LM_BASE_URL", "http://127.0.0.1:1234/v1")

# Model roster (prompts/models.md), verified 2026-08-28.
MODEL_PRIMARY_CODER = "qwen/qwen3-coder-30b"
MODEL_DEEP_CODER = "qwen/qwen2.5-coder-32b"
MODEL_REASONER = "openai/gpt-oss-20b"
MODEL_ROUTER = "qwen3-4b-instruct-2507"  # non-reasoning; the smoke-tested clean one
MODEL_EMBED = "text-embedding-nomic-embed-text-v1.5"

# Guardrails. WALL_CLOCK is recalibrated in triggers.py from BENCH.md.
MAX_CONCURRENT_LANES = 2  # local model wants the GPU; a parallel build wants the CPU
ESCALATIONS_PER_TASK = 4  # beyond this you pay to be wrong more expensively
LOCAL_ATTEMPTS = 2  # two local attempts, then stop
# Hard ceiling on one task's monotonic wall time -- a pathological task must not block
# its wave for the whole night. Generous: ~6 attempts x the 900s per-attempt cap.
TASK_WALL_CAP_S = int(os.environ.get("ZECOR_TASK_WALL_CAP_S", str(6 * 900)))
DISK_FLOOR_GB = 100
UNIT_MAX_FILES = 3
UNIT_MAX_LINES = 150


def ensure_dirs() -> None:
    for d in (TASKS_DIR, LANES_DIR, DIGEST_DIR, LOGS_DIR, DB_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)


def stop_requested() -> bool:
    return STOP_FILE.exists()


TIER_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "claude" / "tier"


def read_tier() -> str:
    """solo | pro | max -- how hard the review step works. Missing means solo."""
    try:
        t = TIER_FILE.read_text().strip().lower()
    except OSError:
        return "solo"
    return t if t in ("solo", "pro", "max") else "solo"