#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# .zecor/verify.sh -- Zecor is a participating repo like any other. exit 0 means done.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --group dev ruff check .
uv run --group dev mypy
uv run --group dev pytest
