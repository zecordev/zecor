# SPDX-License-Identifier: Apache-2.0
"""`python -m zecor` -> the CLI."""

from __future__ import annotations

import sys

from zecor.cli import main

if __name__ == "__main__":
    sys.exit(main())