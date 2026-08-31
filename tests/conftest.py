# SPDX-License-Identifier: Apache-2.0
"""Keep the test suite from writing into the real state tree.

`zecor.config` reads ZECOR_ROOT from the environment at import time, so this has to
happen before any test module imports zecor. conftest.py is imported first, so a
module-level assignment here is early enough.
"""

from __future__ import annotations

import os
import tempfile

if "ZECOR_ROOT" not in os.environ or os.environ["ZECOR_ROOT"] == "":
    os.environ["ZECOR_ROOT"] = tempfile.mkdtemp(prefix="zecor-test-root-")

# The suite models `guarded`/`greenfield` operation -- arbitrary verify commands and
# tier appliers run. `hardened`-mode restrictions are exercised explicitly in
# test_modes.py by setting ZECOR_MODE there.
os.environ.setdefault("ZECOR_MODE", "guarded")