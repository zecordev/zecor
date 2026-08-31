# SPDX-License-Identifier: Apache-2.0
"""Architecture legwork -- mechanical inventory only."""

from __future__ import annotations

import subprocess
from pathlib import Path

from zecor.inventory import inventory, write


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "a.ts").write_text("import { b } from './b';\nexport const a = () => b();\n")
    (root / "src" / "b.ts").write_text("export const b = () => 1;\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    (root / "src" / "b.ts").write_text("export const b = () => 2;\n")
    subprocess.run(["git", "commit", "-aqm", "tweak b"], cwd=root, check=True)
    return root


def test_inventory_has_the_expected_sections(tmp_path: Path) -> None:
    text = inventory(_repo(tmp_path / "r"), "routing")
    assert "# architecture inventory: routing" in text
    assert "not a recommendation" in text
    for section in (
        "Circular dependencies",
        "Cross-package imports",
        "Directory sizes",
        "Most-churned",
    ):
        assert section in text


def test_inventory_reports_churn(tmp_path: Path) -> None:
    text = inventory(_repo(tmp_path / "r"), "x")
    assert "src/b.ts" in text  # b.ts changed twice


def test_write_names_the_file_by_topic(tmp_path: Path) -> None:
    d = tmp_path / "digest"
    path = write(_repo(tmp_path / "r"), "auth flow!", d)
    assert path.name == "architecture-auth-flow.md"
    assert path.exists()