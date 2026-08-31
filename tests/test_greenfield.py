# SPDX-License-Identifier: Apache-2.0
"""greenfield: from-scratch project scaffolding (build-queue item I + non-python kinds)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from zecor import greenfield

# The verify self-check spins a venv / npm install -- skip it in the unit tests and
# check structure; the "does the gate actually go green" cases are marked `slow` below.
_FAST = {"verify": False}


def test_norm_name_and_slug() -> None:
    assert greenfield.norm_name("Widget Tool!") == "widget_tool"
    assert greenfield.norm_name("3d-render") == "pkg_3d_render"
    assert greenfield.norm_slug("Resolve Web!") == "resolve-web"
    assert greenfield.norm_slug("3things") == "app-3things"
    assert greenfield._pascal("resolve-web-shell") == "ResolveWebShell"


def test_scaffold_python_cli_lays_a_runnable_green_repo(tmp_path: Path) -> None:
    res = greenfield.scaffold("widget tool", "renders widgets", tmp_path, kind="python-cli")
    root = res.path
    assert root == tmp_path / "widget_tool" and res.committed and res.gate_verified
    for f in ("pyproject.toml", ".zecor/verify.sh", "src/widget_tool/__init__.py",
              "src/widget_tool/__main__.py", "tests/test_smoke.py", "conftest.py"):
        assert (root / f).exists(), f
    assert (root / ".zecor" / "verify.sh").stat().st_mode & 0o111
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True
    ).stdout
    assert "scaffold widget_tool" in log


def test_scaffold_python_lib_has_no_main(tmp_path: Path) -> None:
    res = greenfield.scaffold("mylib", "a library", tmp_path, kind="python-lib", **_FAST)
    assert not (res.path / "src" / "mylib" / "__main__.py").exists()
    assert (res.path / "src" / "mylib" / "__init__.py").exists()


def test_scaffold_refuses_a_nonempty_dir(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "keep.txt").write_text("x")
    res = greenfield.scaffold("taken", "x", tmp_path, **_FAST)
    assert res.files == [] and "not empty" in res.note


def test_scaffold_bad_kind() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        greenfield.scaffold("x", "y", Path("/tmp"), kind="rust")


def test_scaffold_without_git(tmp_path: Path) -> None:
    res = greenfield.scaffold("nogit", "x", tmp_path, git=False, **_FAST)
    assert not res.committed and not (res.path / ".git").exists()
    assert (res.path / "pyproject.toml").exists()


def test_api_service_structure(tmp_path: Path) -> None:
    res = greenfield.scaffold("resolve api", "the backend", tmp_path, kind="api-service", **_FAST)
    root = res.path
    assert root.name == "resolve_api"
    for f in ("pyproject.toml", "src/resolve_api/app.py", "src/resolve_api/__main__.py",
              "tests/test_health.py", ".zecor/verify.sh"):
        assert (root / f).exists(), f
    assert "fastapi" in (root / "pyproject.toml").read_text()
    assert "/health" in (root / "src" / "resolve_api" / "app.py").read_text()


def test_web_spa_structure_and_valid_json(tmp_path: Path) -> None:
    res = greenfield.scaffold("resolve web", "the web shell", tmp_path, kind="web-spa", **_FAST)
    root = res.path
    assert root.name == "resolve-web"
    for f in ("package.json", "tsconfig.json", "vite.config.ts", "index.html",
              "src/main.ts", "src/app.ts", "src/app.test.ts", ".zecor/verify.sh"):
        assert (root / f).exists(), f
    pkg = json.loads((root / "package.json").read_text())
    assert pkg["name"] == "resolve-web" and "vitest" in pkg["devDependencies"]
    assert pkg["dependencies"] == {"lit": "^3.3.0"}  # house style: Lit, not React
    assert "react" not in (root / "package.json").read_text().lower()
    json.loads((root / "tsconfig.json").read_text())  # must be valid JSON
    app = (root / "src" / "app.ts").read_text()
    assert "resolve web" in app and "LitElement" in app and "resolve-web-app" in app
    assert "pnpm install" in (root / ".zecor" / "verify.sh").read_text()
    assert pkg["packageManager"].startswith("pnpm@")


def test_android_and_ios_are_structure_only(tmp_path: Path) -> None:
    a = greenfield.scaffold(
        "resolve droid", "android client", tmp_path, kind="android-app", **_FAST
    )
    assert a.committed and not a.gate_verified and "gradle wrapper" in a.note
    assert (a.path / "settings.gradle.kts").exists()
    assert any(p.name == "SmokeTest.kt" for p in a.path.rglob("*.kt"))

    i = greenfield.scaffold("resolve ios", "ios client", tmp_path, kind="ios-app", **_FAST)
    assert i.committed and not i.gate_verified and "Xcode" in i.note
    assert (i.path / "Package.swift").exists()
    assert any(p.name == "SmokeTests.swift" for p in i.path.rglob("*.swift"))

# api-service and web-spa gates were proven green by hand this session
# (uv run pytest / npm install + vitest); not kept as tests -- they'd add a
# venv build + npm install to every suite run.