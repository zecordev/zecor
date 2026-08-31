# SPDX-License-Identifier: Apache-2.0
"""Greenfield scaffolding -- build-queue item I.

A from-scratch project has no gate and no code, so `scaffold()` lays down the minimum
-- a package, one trivially-green smoke test, `.zecor/verify.sh`, and a first commit --
after which it's an ordinary participating repo.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("python-cli", "python-lib", "api-service", "web-spa", "android-app", "ios-app")


@dataclass
class ScaffoldResult:
    path: Path
    kind: str
    files: list[str] = field(default_factory=list)
    committed: bool = False
    gate_verified: bool = False  # did scaffold() itself prove the gate goes green?
    note: str = ""


def norm_name(raw: str) -> str:
    """A safe python package name: lowercase, underscores, no leading digit."""
    s = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")
    if not s or s[0].isdigit():
        s = f"pkg_{s}" if s else "pkg"
    return s


def norm_slug(raw: str) -> str:
    """A safe package / directory slug: lowercase, hyphens, no leading digit."""
    s = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")
    if not s or s[0].isdigit():
        s = f"app-{s}" if s else "app"
    return s


def _pascal(slug: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[-_]+", slug) if p) or "App"


_VERIFY = """#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v uv >/dev/null 2>&1; then
  exec uv run --group dev pytest -q
fi
exec python3 -m pytest -q
"""

_SMOKE = '''"""Smoke test -- proves the package imports and the gate runs. Replace me."""

from __future__ import annotations

import {pkg}


def test_package_imports() -> None:
    assert {pkg}.__name__ == "{pkg}"
'''

_INIT_LIB = '"""{pkg} -- {intent}"""\n\n__version__ = "0.0.0"\n'

_INIT_CLI = '"""{pkg} -- {intent}"""\n\n__version__ = "0.0.0"\n'

_MAIN_CLI = '''"""`python -m {pkg}` entry point."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="{pkg}", description="{intent}")
    p.add_argument("--version", action="version", version="0.0.0")
    p.parse_args(argv)
    print("{pkg}: nothing to do yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_PYPROJECT = '''[project]
name = "{pkg}"
version = "0.0.0"
description = "{intent}"
requires-python = ">=3.11"
dependencies = []

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
'''

_README = (
    "# {pkg}\n\n{intent}\n\n"
    "Scaffolded by `zecor scaffold`. `bash .zecor/verify.sh` is the gate.\n"
)

_GITIGNORE = "__pycache__/\n*.pyc\n.venv/\nuv.lock\ndist/\n.pytest_cache/\n"

_CI = """#!/usr/bin/env bash
# CI = the gate, plus a container build when this repo is deployable.
set -euo pipefail
cd "$(dirname "$0")/.."
bash .zecor/verify.sh
if [ -f Dockerfile ]; then
  docker build -q -t "$(basename "$PWD"):ci" . >/dev/null
fi
"""


# -- api-service (FastAPI) ----------------------------------------------------

_API_PYPROJECT = '''[project]
name = "{pkg}"
version = "0.0.0"
description = "{intent}"
requires-python = ">=3.11"
dependencies = ["fastapi>=0.111", "uvicorn>=0.30"]

[dependency-groups]
dev = ["pytest>=8", "httpx>=0.27"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
'''

_API_APP = '''"""{pkg} -- {intent}"""

from __future__ import annotations

from fastapi import FastAPI

__version__ = "0.0.0"
app = FastAPI(title="{pkg}")


@app.get("/health")
def health() -> dict[str, str]:
    return {{"status": "ok"}}


@app.get("/__version")
def version() -> dict[str, str]:
    return {{"version": __version__}}
'''

_API_MAIN = '''"""`python -m {pkg}` -- run the dev server."""

from __future__ import annotations

import uvicorn

from {pkg}.app import app


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
'''

_API_TEST = '''from __future__ import annotations

from fastapi.testclient import TestClient

from {pkg}.app import app

client = TestClient(app)


def test_health_is_ok() -> None:
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {{"status": "ok"}}
'''


# -- web-spa (Vite + Lit web components + TS + Vitest) ---------------------
# House style, mirroring platform-toolkit: Lit custom elements, no React.

_WEB_PKG_JSON = '''{{
  "name": "{slug}",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "packageManager": "pnpm@9.12.0",
  "scripts": {{
    "dev": "vite",
    "build": "tsc -b && vite build",
    "typecheck": "tsc --noEmit",
    "test": "vitest run"
  }},
  "dependencies": {{
    "lit": "^3.3.0"
  }},
  "devDependencies": {{
    "jsdom": "^24.1.1",
    "typescript": "^5.5.4",
    "vite": "^5.4.0",
    "vitest": "^2.0.5"
  }}
}}
'''

_WEB_TSCONFIG = '''{
  "compilerOptions": {
    "target": "ES2022",
    "useDefineForClassFields": false,
    "experimentalDecorators": true,
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "noEmit": true,
    "skipLibCheck": true,
    "types": ["vitest/globals"]
  },
  "include": ["src"]
}
'''

_WEB_VITE_CONFIG = '''import { defineConfig } from "vite";

export default defineConfig({
  test: { environment: "jsdom", globals: true },
});
'''

_WEB_INDEX_HTML = '''<!doctype html>
<html lang="en">
  <head><meta charset="UTF-8" /><title>{slug}</title></head>
  <body>
    <{slug}-app></{slug}-app>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
'''

_WEB_MAIN = 'import "./app";\n'

_WEB_APP = '''import {{ LitElement, css, html }} from "lit";
import {{ customElement }} from "lit/decorators.js";

// {intent}
@customElement("{slug}-app")
export class {pascal}App extends LitElement {{
  static styles = css`
    :host {{ display: block; font: 16px/1.5 system-ui, sans-serif; padding: 1rem; }}
  `;

  render() {{
    return html`<h1>{name}</h1>`;
  }}
}}

declare global {{
  interface HTMLElementTagNameMap {{ "{slug}-app": {pascal}App }}
}}
'''

_WEB_APP_TEST = '''import {{ describe, expect, it }} from "vitest";
import "./app";

describe("{slug}-app", () => {{
  it("renders the product name", async () => {{
    const el = document.createElement("{slug}-app");
    document.body.append(el);
    await (el as any).updateComplete;
    expect(el.shadowRoot?.textContent).toContain("{name}");
    el.remove();
  }});
}});
'''

_WEB_GITIGNORE = "node_modules/\npackage-lock.json\nyarn.lock\ndist/\n*.local\n.vite/\n"

_WEB_VERIFY = '''#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d node_modules ] || pnpm install --frozen-lockfile || pnpm install
pnpm run typecheck
pnpm test
'''


# -- android-app (Kotlin + Gradle skeleton) --------------------------------

_ANDROID_SETTINGS = 'rootProject.name = "{slug}"\ninclude(":app")\n'

_ANDROID_ROOT_BUILD = '''plugins {{
    kotlin("jvm") version "2.0.0" apply false
}}
'''

_ANDROID_APP_BUILD = '''plugins {{
    kotlin("jvm") version "2.0.0"
}}

repositories {{ mavenCentral() }}

dependencies {{
    testImplementation(kotlin("test"))
}}

tasks.test {{ useJUnitPlatform() }}
'''

_ANDROID_MAIN = '''package {pkgid}

// {intent}
fun greeting(): String = "{name}"

fun main() {{
    println(greeting())
}}
'''

_ANDROID_TEST = '''package {pkgid}

import kotlin.test.Test
import kotlin.test.assertEquals

class SmokeTest {{
    @Test
    fun greetingIsTheProductName() {{
        assertEquals("{name}", greeting())
    }}
}}
'''

_ANDROID_VERIFY = '''#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# needs `gradle wrapper` run once + the Android SDK; see README.
./gradlew test
'''

_ANDROID_GITIGNORE = ".gradle/\nbuild/\nlocal.properties\n*.iml\n.idea/\n"

_ANDROID_README = '''# {name}

{intent}

Kotlin/Gradle skeleton scaffolded by `zecor scaffold android-app`. Before the gate can
run: `gradle wrapper` (once, needs a system Gradle) and an Android SDK / `local.properties`.
`./gradlew test` is then the gate.
'''


# -- ios-app (SwiftPM package + XCTest) ------------------------------------

_IOS_PACKAGE = '''// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "{pascal}",
    targets: [
        .target(name: "{pascal}"),
        .testTarget(name: "{pascal}Tests", dependencies: ["{pascal}"]),
    ]
)
'''

_IOS_SRC = '''// {intent}
public func greeting() -> String {{ "{name}" }}
'''

_IOS_TEST = '''import XCTest
@testable import {pascal}

final class SmokeTests: XCTestCase {{
    func testGreetingIsTheProductName() {{
        XCTAssertEqual(greeting(), "{name}")
    }}
}}
'''

_IOS_VERIFY = '''#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
swift test
'''

_IOS_GITIGNORE = ".build/\n*.xcodeproj/\nDerivedData/\n.swiftpm/\n"

_IOS_README = '''# {name}

{intent}

SwiftPM package + XCTest scaffolded by `zecor scaffold ios-app`. `swift test` is the
gate (runs on macOS without full Xcode). Wrap it in an Xcode app target when there is UI.
'''


@dataclass(frozen=True)
class _Spec:
    proj: str  # directory name
    files: dict[str, str]
    verify: str
    selfcheck: list[str] | None  # a command that proves the gate is green, or None
    note: str


def _spec(kind: str, name: str, intent: str) -> _Spec:
    if kind in ("python-cli", "python-lib"):
        pkg = norm_name(name)
        files = {
            "pyproject.toml": _PYPROJECT.format(pkg=pkg, intent=intent),
            "README.md": _README.format(pkg=pkg, intent=intent),
            ".gitignore": _GITIGNORE,
            ".zecor/verify.sh": _VERIFY,
            f"src/{pkg}/__init__.py": (_INIT_CLI if kind == "python-cli" else _INIT_LIB).format(
                pkg=pkg, intent=intent
            ),
            "tests/__init__.py": "",
            "tests/test_smoke.py": _SMOKE.format(pkg=pkg),
            "conftest.py": 'import sys\nfrom pathlib import Path\n'
            'sys.path.insert(0, str(Path(__file__).parent / "src"))\n',
        }
        if kind == "python-cli":
            files[f"src/{pkg}/__main__.py"] = _MAIN_CLI.format(pkg=pkg, intent=intent)
        return _Spec(pkg, files, _VERIFY, ["bash", ".zecor/verify.sh"], "")

    if kind == "api-service":
        pkg = norm_name(name)
        files = {
            "pyproject.toml": _API_PYPROJECT.format(pkg=pkg, intent=intent),
            "README.md": _README.format(pkg=pkg, intent=intent),
            ".gitignore": _GITIGNORE,
            ".zecor/verify.sh": _VERIFY,
            f"src/{pkg}/__init__.py": f'"""{pkg} -- {intent}"""\n',
            f"src/{pkg}/app.py": _API_APP.format(pkg=pkg, intent=intent),
            f"src/{pkg}/__main__.py": _API_MAIN.format(pkg=pkg),
            "tests/__init__.py": "",
            "tests/test_health.py": _API_TEST.format(pkg=pkg),
            ".zecor/ci.sh": _CI,
            "conftest.py": 'import sys\nfrom pathlib import Path\n'
            'sys.path.insert(0, str(Path(__file__).parent / "src"))\n',
        }
        return _Spec(pkg, files, _VERIFY, ["bash", ".zecor/verify.sh"], "")

    if kind == "web-spa":
        slug = norm_slug(name)
        pascal = _pascal(slug)
        files = {
            "package.json": _WEB_PKG_JSON.format(slug=slug),
            "tsconfig.json": _WEB_TSCONFIG,
            "vite.config.ts": _WEB_VITE_CONFIG,
            "index.html": _WEB_INDEX_HTML.format(slug=slug),
            ".gitignore": _WEB_GITIGNORE,
            ".zecor/verify.sh": _WEB_VERIFY,
            "src/main.ts": _WEB_MAIN,
            "src/app.ts": _WEB_APP.format(name=name, intent=intent, slug=slug, pascal=pascal),
            "src/app.test.ts": _WEB_APP_TEST.format(name=name, slug=slug),
            ".zecor/ci.sh": _CI,
            "README.md": _README.format(pkg=slug, intent=intent),
        }
        check = ["bash", ".zecor/verify.sh"] if shutil.which("pnpm") else None
        return _Spec(
            slug, files, _WEB_VERIFY, check,
            "" if check else "pnpm not on PATH -- gate unverified by scaffold",
        )

    if kind == "android-app":
        slug = norm_slug(name)
        pkgid = "app." + re.sub(r"[^a-z0-9]+", "", slug)
        kroot = f"app/src/main/kotlin/{pkgid.replace('.', '/')}"
        ktest = f"app/src/test/kotlin/{pkgid.replace('.', '/')}"
        files = {
            "settings.gradle.kts": _ANDROID_SETTINGS.format(slug=slug),
            "build.gradle.kts": _ANDROID_ROOT_BUILD,
            "app/build.gradle.kts": _ANDROID_APP_BUILD,
            f"{kroot}/Main.kt": _ANDROID_MAIN.format(pkgid=pkgid, name=name, intent=intent),
            f"{ktest}/SmokeTest.kt": _ANDROID_TEST.format(pkgid=pkgid, name=name),
            ".gitignore": _ANDROID_GITIGNORE,
            ".zecor/verify.sh": _ANDROID_VERIFY,
            "README.md": _ANDROID_README.format(name=name, intent=intent),
        }
        return _Spec(slug, files, _ANDROID_VERIFY, None,
                     "gate needs `gradle wrapper` + the Android SDK -- unverified")

    if kind == "ios-app":
        slug = norm_slug(name)
        pascal = _pascal(slug)
        files = {
            "Package.swift": _IOS_PACKAGE.format(pascal=pascal),
            f"Sources/{pascal}/{pascal}.swift": _IOS_SRC.format(name=name, intent=intent),
            f"Tests/{pascal}Tests/SmokeTests.swift": _IOS_TEST.format(pascal=pascal, name=name),
            ".gitignore": _IOS_GITIGNORE,
            ".zecor/verify.sh": _IOS_VERIFY,
            "README.md": _IOS_README.format(name=name, intent=intent),
        }
        return _Spec(
            slug, files, _IOS_VERIFY, None,
            "gate needs full Xcode (XCTest) -- structure only, unverified",
        )

    raise ValueError(f"kind must be one of {KINDS}")


def scaffold(
    name: str,
    intent: str,
    dest: Path,
    *,
    kind: str = "python-cli",
    git: bool = True,
    verify: bool = True,
) -> ScaffoldResult:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    intent = " ".join(intent.split()) or "(no description)"
    spec = _spec(kind, name, intent)
    root = dest / spec.proj
    res = ScaffoldResult(path=root, kind=kind)
    if root.exists() and any(root.iterdir()):
        res.note = f"{root} exists and is not empty -- refusing to scaffold over it"
        return res

    for rel, content in spec.files.items():
        fp = root / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
    for sh in (".zecor/verify.sh", ".zecor/ci.sh"):
        if (root / sh).exists():
            (root / sh).chmod(0o755)
    res.files = sorted(spec.files)

    # commit FIRST, from the pristine tree -- so a self-check that runs `uv run` / `pnpm
    # install` cannot land a lockfile or a venv in the initial commit.
    if git:
        try:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, timeout=30)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True, timeout=30)
            # -c user.* so this commits on a host with no git identity configured (a
            # fresh CI runner, a bot account) -- an automated scaffold commit, not a
            # human one, so it shouldn't depend on ambient global config either way.
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "-c", "user.name=zecor",
                 "-c", "user.email=zecor@localhost", "commit", "-qm",
                 f"scaffold {spec.proj}: {intent}"],
                cwd=root, check=True, timeout=30,
            )
            res.committed = True
        except (OSError, subprocess.SubprocessError) as e:
            res.note = res.note or f"scaffolded but git init/commit failed: {e}"

    if verify and spec.selfcheck is not None:
        try:
            r = subprocess.run(
                spec.selfcheck, cwd=root, capture_output=True, text=True, timeout=600
            )
            res.gate_verified = r.returncode == 0
            if not res.gate_verified:
                res.note = f"gate did NOT pass: {(r.stdout + r.stderr).strip()[-400:]}"
        except (OSError, subprocess.SubprocessError) as e:
            res.note = f"gate self-check could not run: {e}"

    res.note = res.note or spec.note or (
        f"scaffolded {kind} at {root} ({len(res.files)} files"
        f"{'; gate green' if res.gate_verified else ''})"
    )
    return res