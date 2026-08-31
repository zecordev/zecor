# SPDX-License-Identifier: Apache-2.0
"""Deep self-review of Zecor's *output*: a gate-quality probe, an adversarial
frontier pass, and a `block` verdict that flips the outcome to needs-human.
Advisory-plus -- the gate already passed, so the commit stays either way."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from zecor.cards import Card
from zecor.frontier import NoFrontierAvailable, ask
from zecor.jsonx import first_object
from zecor.review import (
    Finding,
    ReviewResult,
    _files_in_diff,
    _norm_line,
    _norm_severity,
    review_branch,
)
from zecor.task import Klass
from zecor.workers.base import run_capped

_NARROW_GATE = re.compile(r"(-k[ =]|::|test_[\w/]+\.py|/test_|_test\.py|--tests?\b)")

# The local coder tends to fix adjacent bugs it sees while it's in a file, producing
# scope creep past the card's one behaviour change. Override with ZECOR_SYMBOL_SCOPE_MAX.
SYMBOL_SCOPE_MAX = max(1, int(os.environ.get("ZECOR_SYMBOL_SCOPE_MAX", "3")))
_BROAD_INTENT = re.compile(r"\b(refactor|rename|renam|migrat|sweep|reformat|bulk|reorganiz)", re.I)

# Tuned high enough that ordinary code clears these -- for the 200-line, deeply-nested
# function a model emits when it should have split the work.
COMPLEXITY_MAX = max(1, int(os.environ.get("ZECOR_COMPLEXITY_MAX", "20")))
FN_LINES_MAX = max(1, int(os.environ.get("ZECOR_FN_LINES_MAX", "120")))
FN_DEPTH_MAX = max(1, int(os.environ.get("ZECOR_FN_DEPTH_MAX", "5")))


def should_deep_review(card: Card, tiers_used: list[str], files_changed: int) -> bool:
    """A green gate is weak evidence when the change is multi-file, needed a frontier
    tier (if local could not build it, local cannot judge it), or the gate is narrow."""
    if files_changed > 1:
        return True
    if any(t not in ("t0", "t1") for t in tiers_used):
        return True
    return bool(_NARROW_GATE.search(card.verify_cmd))


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.spec\.", re.I
)


def gate_quality(verify_cmd: str, lane_path: Path, base_sha: str, diff: str) -> Finding | None:
    """Reverts the non-test part of the change and runs the gate; still GREEN means the
    implementation did not matter, a tautological gate. Runs in a throwaway worktree so
    the lane itself is never touched."""
    files = _files_in_diff(diff)
    non_test = [f for f in files if not _TEST_PATH.search(f)]
    if not non_test:
        return None  # test-only change -- nothing to revert, nothing to probe
    ok_base = _git(["rev-parse", "--verify", "--quiet", f"{base_sha}^{{commit}}"], lane_path)
    if not base_sha or ok_base.returncode != 0:
        return Finding(
            lens="gate-quality", severity="medium", file=non_test[0], line=0,
            summary="could not resolve the lane's base commit -- gate-quality probe skipped",
            verdict="PLAUSIBLE",
        )
    test_files = [f for f in files if _TEST_PATH.search(f)]
    lane_head = _git(["rev-parse", "HEAD"], lane_path).stdout.strip() or "HEAD"
    with tempfile.TemporaryDirectory(prefix="zecor-gq-") as td:
        wt = Path(td) / "wt"
        add = _git(["worktree", "add", "--detach", "--force", str(wt), base_sha], lane_path)
        if add.returncode != 0:
            return None
        try:
            for tf in test_files:  # bring the POST-fix tests into the pre-fix tree
                _git(["checkout", lane_head, "--", tf], wt)
            rc, _out, _err, killed = run_capped(
                ["/bin/sh", "-c", verify_cmd], cwd=wt, timeout_s=300.0
            )
            impl_did_not_matter = (not killed) and rc == 0
        finally:
            _git(["worktree", "remove", "--force", str(wt)], lane_path)
    if impl_did_not_matter:
        return Finding(
            lens="gate-quality", severity="high", file=non_test[0], line=0,
            summary=(
                "the gate still passes with the implementation reverted (tests kept) -- "
                "the change does not actually make anything pass; a green land proves nothing"
            ),
            verdict="CONFIRMED",
        )
    return None


_ADVERSARIAL = (
    "This unified diff PASSED its automated gate. Assume it is nonetheless subtly wrong, "
    "incomplete, or over-broad. Task intent: {intent}\nNon-goals: {non_goals}\n\n"
    "Find real defects only -- an off-by-one, an unhandled input the tests miss, a "
    "behaviour change outside the intent, a resource leak, a security issue. Do NOT "
    "restate what the diff does. Reply JSON: "
    '{{"findings": [{{"severity": "low|medium|high", "file": "", "line": 0, "summary": ""}}]}}\n\n'
    "{diff}"
)


def _adversarial_pass(card: Card, diff: str) -> tuple[list[Finding], str]:
    prompt = _ADVERSARIAL.format(
        intent=card.intent,
        non_goals="; ".join(card.non_goals) or "(none)",
        diff=diff,
    )
    try:
        text, tier = ask(prompt, timeout_s=300.0)
    except NoFrontierAvailable:
        return [], ""
    try:
        raw = first_object(text).get("findings", [])
    except ValueError:
        return [], tier
    out: list[Finding] = []
    if isinstance(raw, list):
        for f in raw:
            if isinstance(f, dict) and str(f.get("summary", "")).strip():
                out.append(
                    Finding(
                        lens="adversarial",
                        severity=_norm_severity(f.get("severity", "medium")),
                        file=str(f.get("file", "?")),
                        line=_norm_line(f.get("line", 0)),
                        summary=str(f.get("summary", "")).strip(),
                        verdict="PLAUSIBLE",
                    )
                )
    return out, tier


def _corroborates(f: Finding, existing: list[Finding]) -> bool:
    """Fuzzy: same file and an overlapping keyword in the summary."""
    words = {w for w in re.findall(r"[a-z]{4,}", f.summary.lower())}
    for e in existing:
        if e.file == f.file and words & {w for w in re.findall(r"[a-z]{4,}", e.summary.lower())}:
            return True
    return False


def _changed_new_lines(diff: str) -> dict[str, set[int]]:
    """Per file, the line numbers in the POST-change file that a hunk added or sat
    astride a deletion -- enough to ask "which symbol did this touch"."""
    out: dict[str, set[int]] = {}
    cur: str | None = None
    new_ln = 0
    for line in diff.splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            cur = None if m.group(1) == "/dev/null" else m.group(1)
            continue
        h = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if h:
            new_ln = int(h.group(1))
            continue
        if cur is None or line.startswith(("diff --git", "--- ", "index ", "\\ ")):
            continue
        if line.startswith("+"):
            out.setdefault(cur, set()).add(new_ln)
            new_ln += 1
        elif line.startswith("-"):
            out.setdefault(cur, set()).add(new_ln)  # deletion sits at this new-file offset
        elif line.startswith(" "):
            new_ln += 1
    return out


def _module_symbols(source: str) -> list[tuple[str, int, int]]:
    """(name, first_line, last_line) for every module-level def/class/assignment."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    syms: list[tuple[str, int, int]] = []
    for node in tree.body:
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            syms.append((node.name, node.lineno, end))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    syms.append((t.id, node.lineno, end))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            syms.append((node.target.id, node.lineno, end))
    return syms


def symbol_scope_findings(card: Card, diff: str, lane_path: Path) -> list[Finding]:
    """Flags a diff that edits more module-level symbols than a bounded change should.
    Advisory only, and skipped for unbounded work and refactor-shaped asks."""
    if card.klass is Klass.UNBOUNDED or _BROAD_INTENT.search(card.intent or ""):
        return []
    from zecor import astscan

    changed = _changed_new_lines(diff)
    touched: list[str] = []
    for path, lines in changed.items():
        if _TEST_PATH.search(path) or not astscan.is_source(path):
            continue
        try:
            source = (lane_path / path).read_text()
        except OSError:
            continue
        if path.endswith((".py", ".pyi")):
            spans = [(n, lo, hi) for n, lo, hi in _module_symbols(source)]
        else:
            # non-Python: line spans from the Rust Tree-sitter symbol list
            spans = []
            for s in astscan.symbols(lane_path, path):
                end_row = s.row + source[s.start_byte:s.end_byte].count("\n")
                spans.append((s.name, s.row, end_row))
        for name, lo, hi in spans:
            if any(lo <= n <= hi for n in lines):
                touched.append(name)
    uniq = sorted(set(touched))
    if len(uniq) <= SYMBOL_SCOPE_MAX:
        return []
    return [
        Finding(
            lens="scope",
            severity="medium",
            file=next(
                (p for p in changed if astscan.is_source(p) and not _TEST_PATH.search(p)), "?"
            ),
            line=0,
            summary=(
                f"edits {len(uniq)} module-level symbols ({', '.join(uniq[:6])}"
                f"{', ...' if len(uniq) > 6 else ''}) -- a bounded card is one behaviour "
                "change; confirm the extra edits are in scope and not adjacent-bug drift"
            ),
            verdict="PLAUSIBLE",
        )
    ]


def complexity_findings(diff: str, lane_path: Path) -> list[Finding]:
    """Flags a touched function past the complexity/length/depth caps. Advisory only,
    and silent when `zecor-astscan` is not built (metrics is Rust-only)."""
    from zecor import astscan

    changed = _changed_new_lines(diff)
    out: list[Finding] = []
    for path, lines in changed.items():
        if _TEST_PATH.search(path) or not astscan.is_source(path):
            continue
        for fn in astscan.metrics(lane_path, path):
            span = range(fn.row, fn.row + max(fn.lines, 1))
            if not any(n in span for n in lines):
                continue
            hits = []
            if fn.complexity > COMPLEXITY_MAX:
                hits.append(f"cyclomatic complexity {fn.complexity}")
            if fn.lines > FN_LINES_MAX:
                hits.append(f"{fn.lines} lines")
            if fn.max_depth > FN_DEPTH_MAX:
                hits.append(f"nested {fn.max_depth} deep")
            if hits:
                out.append(Finding(
                    lens="complexity",
                    severity="medium",
                    file=path,
                    line=fn.row,
                    summary=(
                        f"`{fn.name}` is {', '.join(hits)} -- confirm this is intended and "
                        "not a change that should have been split"
                    ),
                    verdict="PLAUSIBLE",
                ))
    return out


def deep_review(
    card: Card,
    diff: str,
    *,
    lane_path: Path,
    base_sha: str,
    ledger_has_frontier: bool,
    record_frontier: Callable[[str], None] | None = None,
    frontier_ok: Callable[[], bool] | None = None,
) -> ReviewResult:
    """review_branch plus the gate-quality probe plus one adversarial frontier pass,
    merged with an agreement filter; a gate-quality hit forces `block`. `frontier_ok()`
    is re-checked before the adversarial pass so a filled window skips it -- one ledger
    check does not authorize four calls."""
    r = review_branch(card, diff, ledger_has_frontier=ledger_has_frontier)
    if record_frontier and r.tier_used in ("t3", "t4"):
        record_frontier(r.tier_used)

    if card.allow_files:
        allowed = set(card.allow_files)
        for f in _files_in_diff(diff):
            if f not in allowed:
                r.findings.append(
                    Finding(
                        lens="scope",
                        severity="medium",
                        file=f,
                        line=0,
                        summary="changed outside the card's allow-list "
                        f"({', '.join(card.allow_files)})",
                        verdict="CONFIRMED",
                    )
                )
        if any(f.lens == "scope" for f in r.findings) and r.verdict in (
            "ship", "local-only", "no-review",
        ):
            r.verdict = "needs-work"

    sym = symbol_scope_findings(card, diff, lane_path)
    if sym:
        r.findings.extend(sym)
        if r.verdict in ("ship", "local-only", "no-review"):
            r.verdict = "needs-work"

    cx = complexity_findings(diff, lane_path)
    if cx:
        r.findings.extend(cx)
        if r.verdict in ("ship", "local-only", "no-review"):
            r.verdict = "needs-work"

    gq = gate_quality(card.verify_cmd, lane_path, base_sha, diff)
    if gq is not None:
        r.findings.append(gq)
        r.verdict = "block"
        r.skipped = (r.skipped + "; gate-quality: reverted diff still passes").strip("; ")

    do_adversarial = ledger_has_frontier and (frontier_ok is None or frontier_ok())
    if do_adversarial:
        extra, tier2 = _adversarial_pass(card, diff)
        if record_frontier and tier2 in ("t3", "t4"):
            record_frontier(tier2)
        kept = [f for f in extra if f.severity == "high" or _corroborates(f, r.findings)]
        r.findings.extend(kept)
        if tier2:
            r.tier_used = (r.tier_used + f"+{tier2}").strip("+")
        if r.verdict in ("ship", "local-only", "no-review") and any(
            f.severity == "high" for f in kept
        ):
            r.verdict = "needs-work"

    return r