# SPDX-License-Identifier: Apache-2.0
"""T0 -- the deterministic rule executor.

An active rule is an `ast-grep` rule file with a small zecor header:

    rules/active/drop-not-implemented.yml
    -------------------------------------
    id: drop-not-implemented
    language: python
    # zecor: repos = *          (glob or comma list; default: any)
    # zecor: intent = ~stub|placeholder   (optional regex the card intent must match)
    rule:
      pattern: "raise NotImplementedError"
    fix: "pass"

`route()` sends a card to T0 only when a matching active rule exists; T0 applying
nothing is a `refused` outcome, so the orchestrator escalates to T1 as if a model had
declined.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from zecor import config

AST_GREP = "ast-grep"
_HDR_REPOS = re.compile(r"^#\s*zecor:\s*repos\s*=\s*(.+?)\s*$", re.M)
_HDR_INTENT = re.compile(r"^#\s*zecor:\s*intent\s*=\s*~(.+?)\s*$", re.M)


@dataclass(frozen=True)
class Rule:
    path: Path
    rule_id: str
    repos: tuple[str, ...]  # globs; ("*",) means any
    intent_re: str | None

    def matches(self, repo: str, intent: str) -> bool:
        if not any(fnmatch.fnmatch(repo, g) for g in self.repos):
            return False
        return not (self.intent_re and not re.search(self.intent_re, intent, re.I))


def _parse(path: Path) -> Rule | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    rid_m = re.search(r"^id:\s*(\S+)", text, re.M)
    if not rid_m:
        return None
    repos_m = _HDR_REPOS.search(text)
    repos = (
        tuple(x for x in re.split(r"[,\s]+", repos_m.group(1)) if x) if repos_m else ("*",)
    )
    intent_m = _HDR_INTENT.search(text)
    return Rule(path, rid_m.group(1), repos, intent_m.group(1) if intent_m else None)


def active_dir(rules_dir: Path | None = None) -> Path:
    return (rules_dir or config.RULES_DIR) / "active"


def load_active(rules_dir: Path | None = None) -> list[Rule]:
    d = active_dir(rules_dir)
    if not d.is_dir():
        return []
    out: list[Rule] = []
    for f in sorted(d.glob("*.yml")):
        r = _parse(f)
        if r is not None:
            out.append(r)
    return out


def select(rules: list[Rule], repo: str, intent: str) -> list[Rule]:
    return [r for r in rules if r.matches(repo, intent)]


@dataclass(frozen=True)
class ApplyResult:
    changed: tuple[str, ...]  # repo-relative paths ast-grep rewrote
    rules_run: tuple[str, ...]
    note: str


def _changed_files(cwd: Path) -> list[str]:
    r = subprocess.run(
        ["git", "diff", "--name-only"], cwd=cwd, capture_output=True, text=True, check=False
    )
    return [p for p in r.stdout.splitlines() if p.strip()]


def apply(rules: list[Rule], lane_dir: Path, *, timeout_s: float = 120.0) -> ApplyResult:
    """Run every rule's rewrite over `lane_dir` in place, then report which files moved.
    ast-grep only edits an exact structural match, so a rule that matches nothing is a
    no-op -- the caller reads an empty `changed` as 'T0 had nothing to do'."""
    ran: list[str] = []
    for r in rules:
        try:
            subprocess.run(
                [AST_GREP, "scan", "--rule", str(r.path), "--update-all", str(lane_dir)],
                capture_output=True, text=True, check=False, timeout=timeout_s,
            )
            ran.append(r.rule_id)
        except (OSError, subprocess.SubprocessError):
            continue
    changed = _changed_files(lane_dir)
    note = (
        f"T0 applied {', '.join(ran)} -> {len(changed)} file(s)"
        if changed
        else f"T0: {', '.join(ran) or 'no rules'} matched nothing"
    )
    return ApplyResult(tuple(changed), tuple(ran), note)


def available() -> bool:
    try:
        subprocess.run([AST_GREP, "--version"], capture_output=True, check=False, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def has_rule_for(repo: str, intent: str, *, rules_dir: Path | None = None) -> bool:
    """Planner hook: is there a confirmed active rule this card could be handed to T0?"""
    return bool(select(load_active(rules_dir), repo, intent))