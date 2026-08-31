# SPDX-License-Identifier: Apache-2.0
"""zecor.secretscan tree walk + fingerprint + SARIF (the CI-facing surface)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from zecor import _rustbridge, secretscan


def test_fingerprint_is_line_independent() -> None:
    a = secretscan.Finding("github-token", "app.py", 3, "ghp_…zz")
    b = secretscan.Finding("github-token", "app.py", 99, "ghp_…zz")
    assert secretscan.fingerprint(a) == secretscan.fingerprint(b)
    assert len(secretscan.fingerprint(a)) == 16


def test_fingerprint_matches_rust_when_built(tmp_path: Path, monkeypatch) -> None:
    if _rustbridge.binary("zecor-secretscan") is None:
        import pytest

        pytest.skip("zecor-secretscan not built")
    (tmp_path / "c.py").write_text('key = "ghp_' + "a" * 40 + '"\n')
    b = _rustbridge.binary("zecor-secretscan")
    assert b is not None
    # `baseline DIR` prints `<fp>  # rule file:line`, one per accepted finding
    out = subprocess.run(
        [b, "baseline", str(tmp_path)], capture_output=True, text=True, check=False
    )
    rust_fps = {ln.split()[0] for ln in out.stdout.splitlines() if ln and not ln.startswith("#")}
    monkeypatch.setenv("ZECOR_NO_RUST", "1")
    _rustbridge.binary.cache_clear()
    py = secretscan.scan_text((tmp_path / "c.py").read_text(), file="c.py")
    _rustbridge.binary.cache_clear()
    assert rust_fps == {secretscan.fingerprint(f) for f in py}


def test_scan_tree_walks_and_honours_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("vendored/\n")
    (tmp_path / "vendored").mkdir()
    (tmp_path / "vendored" / "leak.env").write_text("K=ghp_" + "a" * 40)
    (tmp_path / "app.py").write_text('K = "ghp_' + "b" * 40 + '"')
    findings = secretscan.scan_tree(str(tmp_path))
    assert [f.file for f in findings] == ["app.py"]


def test_scan_cli_sarif(tmp_path: Path) -> None:
    f = tmp_path / "c.py"
    f.write_text('token = "ghp_' + "a" * 40 + '"\n')
    out = subprocess.run(
        [sys.executable, "-m", "zecor", "scan", str(f), "--sarif"],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 1
    doc = json.loads(out.stdout)
    assert doc["version"] == "2.1.0"
    res = doc["runs"][0]["results"]
    assert res and res[0]["ruleId"] == "github-token"
    assert res[0]["locations"][0]["physicalLocation"]["region"]["startLine"] == 1


def test_scan_cli_dir(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text('k = "ghp_' + "z" * 40 + '"\n')
    out = subprocess.run(
        [sys.executable, "-m", "zecor", "scan", "--dir", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 1
    assert "github-token" in out.stdout
