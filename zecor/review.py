# SPDX-License-Identifier: Apache-2.0
"""The review step, two passes, both cheap: (1) a local lens pass at T1, with REFUTED
findings dropped before anything costs a frontier token; (2) one batched frontier review
per branch that adjudicates those findings and applies the lenses that only run well at
the frontier. Advisory only -- the gate already passed and the PR stays a draft until a
human marks it ready."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from zecor.cards import Card
from zecor.config import MODEL_REASONER, REPO_ROOT, read_tier
from zecor.frontier import NoFrontierAvailable, ask
from zecor.jsonx import first_object
from zecor.localmodel import LocalModelError, available, complete

LENS_DIR = REPO_ROOT / "lenses" / "agents"
ALL_LENSES = ("correctness", "tests", "security", "performance", "accessibility", "prose")
LOCAL_LENSES = frozenset({"accessibility", "prose", "tests"})
HIGH_STAKES = re.compile(
    r"auth|login|password|token|secret|payment|billing|migrat|money|crypto", re.I
)

_FINDINGS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "file", "line", "summary"],
                "properties": {
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "summary": {"type": "string"},
                },
            },
        }
    },
}

_VERIFY_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason"],
    "properties": {
        "verdict": {"type": "string", "enum": ["CONFIRMED", "REFUTED", "PLAUSIBLE"]},
        "reason": {"type": "string"},
    },
}


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_SEVERITY_ALIASES = {
    "critical": "high",
    "blocker": "high",
    "major": "high",
    "moderate": "medium",
    "warning": "medium",
    "minor": "low",
    "info": "low",
    "note": "low",
    "nit": "low",
    "": "medium",
}


def _norm_severity(raw: object) -> str:
    """Clamp any label a model hands back to low|medium|high. A frontier reply is not
    schema-bound, so 'critical'/'warning'/'' must not reach a bare dict lookup."""
    s = str(raw).strip().lower()
    if s in SEVERITY_ORDER:
        return s
    return _SEVERITY_ALIASES.get(s, "medium")


_VERDICTS = ("CONFIRMED", "REFUTED", "PLAUSIBLE")


def _norm_verdict(raw: object) -> str:
    """A frontier/local verdict is unschema'd text. Anything we do not recognise is
    PLAUSIBLE -- never silently 'not REFUTED' on a case-mismatch."""
    s = str(raw).strip().upper()
    return s if s in _VERDICTS else "PLAUSIBLE"


def _norm_line(raw: object) -> int:
    """Model line numbers arrive as 12, "12", "12-15", "L12", "" -- take the first run
    of digits, default 0. Never raise on the land path."""
    digits = ""
    for ch in str(raw):
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else 0


@dataclass
class Finding:
    lens: str
    severity: str
    file: str
    line: int
    summary: str
    verdict: str = ""  # CONFIRMED | PLAUSIBLE after local verify; REFUTED ones are dropped

    def __post_init__(self) -> None:
        self.severity = _norm_severity(self.severity)


@dataclass
class ReviewResult:
    verdict: str = "no-review"  # ship | needs-work | block | local-only | no-review
    lenses_run: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    frontier_notes: str = ""
    tier_used: str = ""
    skipped: str = ""

    def summary_line(self) -> str:
        hi = sum(1 for f in self.findings if f.severity == "high")
        return (
            f"review: {self.verdict}  ({len(self.findings)} finding(s), {hi} high; "
            f"lenses {'+'.join(self.lenses_run) or 'none'}"
            + (f"; frontier {self.tier_used}" if self.tier_used else "")
            + (f"; {self.skipped}" if self.skipped else "")
            + ")"
        )

    def as_markdown(self) -> str:
        out = [f"**Review — {self.verdict}**", ""]
        if self.findings:
            for f in sorted(
                self.findings, key=lambda x: SEVERITY_ORDER.get(x.severity, 1)
            ):
                mark = "" if f.verdict == "CONFIRMED" else f" _{f.verdict.lower()}_"
                out.append(f"- **{f.severity}** `{f.file}:{f.line}` ({f.lens}){mark} — {f.summary}")
        else:
            out.append("- no findings")
        if self.frontier_notes:
            out += ["", self.frontier_notes.strip()]
        return "\n".join(out)


def pick_lenses(files: list[str]) -> list[str]:
    """Diff shape -> lenses."""
    joined = " ".join(files).lower()

    def only(pat: str) -> bool:
        return bool(files) and all(re.search(pat, f, re.I) for f in files)

    def any_(pat: str) -> bool:
        return bool(re.search(pat, joined, re.I))

    if only(r"\.(md|mdx)$|(^|/)docs/"):
        return ["prose"]
    if only(r"\.(test|spec)\.[jt]sx?$|(^|/)tests?/|__tests__"):
        return ["tests"]
    if any_(
        r"(^|/)package(-lock)?\.json$|yarn\.lock|pnpm-lock|pyproject\.toml|requirements.*\.txt"
    ):
        return ["security", "performance"]
    if any_(r"\.tsx$|\.jsx$|\.vue$|\.svelte$|(^|/)components?/|(^|/)ui/"):
        return ["accessibility", "correctness", "tests"]
    if any_(r"(^|/)api/|(^|/)db/|storage|serial|schema|migrat|\.sql$"):
        return ["security", "correctness", "tests"]
    if any_(r"\.github/|dockerfile|makefile|\.sh$|vite\.config|webpack|rollup|\.ya?ml$"):
        return ["tests", "performance", "correctness"]
    return ["correctness", "tests"]


def load_lens(name: str) -> str:
    """The lens agent file, minus YAML frontmatter."""
    body = (LENS_DIR / f"{name}.md").read_text()
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return body.strip()


def _run_lens_local(name: str, diff: str, card: Card) -> list[Finding]:
    system = (
        load_lens(name)
        + "\n\n---\nApply the lens above to the unified diff below. Reply as JSON matching "
        "the schema: a `findings` array, each with severity (low|medium|high), file, line, "
        "and a one-sentence summary. Empty array if the lens has nothing to say. Judge the "
        "change only against its stated task; do not flag pre-existing issues the diff "
        "merely touches."
    )
    prompt = (
        f"Task: {card.intent}\nAllowed files: {', '.join(card.allow_files) or '(any)'}\n\n{diff}"
    )
    try:
        out = complete(
            prompt,
            model=MODEL_REASONER,
            system=system,
            max_tokens=900,
            temperature=0.0,
            json_schema=_FINDINGS_SCHEMA,
        )
        raw = first_object(out.text).get("findings", [])
    except (LocalModelError, ValueError):
        return []
    found: list[Finding] = []
    if isinstance(raw, list):
        for f in raw:
            if isinstance(f, dict):
                found.append(
                    Finding(
                        lens=name,
                        severity=str(f.get("severity", "low")),
                        file=str(f.get("file", "?")),
                        line=_norm_line(f.get("line", 0)),
                        summary=str(f.get("summary", "")).strip(),
                    )
                )
    return found


def _verify_local(finding: Finding, diff: str) -> str:
    system = (
        load_lens("correctness")
        + "\n\n---\nYou are given ONE finding and the diff it refers to. Try to REFUTE it: "
        "is the cited line on a path that executes; is the mechanism real; was it already "
        "broken before this change; is it handled somewhere the finder did not look. Reply "
        "as JSON {verdict: CONFIRMED|REFUTED|PLAUSIBLE, reason: <one line>}. A failed "
        "reproduction is PLAUSIBLE, not REFUTED."
    )
    prompt = (
        f"Finding ({finding.lens}, {finding.severity}) at {finding.file}:{finding.line}\n"
        f"{finding.summary}\n\n{diff}"
    )
    try:
        out = complete(
            prompt,
            model=MODEL_REASONER,
            system=system,
            max_tokens=200,
            temperature=0.0,
            json_schema=_VERIFY_SCHEMA,
        )
        return _norm_verdict(first_object(out.text).get("verdict", "PLAUSIBLE"))
    except (LocalModelError, ValueError):
        return "PLAUSIBLE"


def _frontier_review(
    diff: str, card: Card, findings: list[Finding], remaining_lenses: list[str]
) -> tuple[str, str, list[Finding], str, str]:
    findings_txt = (
        "\n".join(
            f"- [{f.lens}/{f.severity}] {f.file}:{f.line} ({f.verdict or 'unverified'}) {f.summary}"
            for f in findings
        )
        or "(none from the local pass)"
    )
    allowed = ", ".join(card.allow_files) or "(any)"
    non_goals = "; ".join(card.non_goals) or "(none)"
    also = ", ".join(remaining_lenses) or "(none)"
    prompt = (
        "Review this branch. Task card:\n"
        f"  intent: {card.intent}\n"
        f"  verify: {card.verify_cmd}\n"
        f"  allowed files: {allowed}\n"
        f"  non-goals: {non_goals}\n\n"
        "Local lens findings -- adjudicate each (keep / drop / adjust severity):\n"
        f"{findings_txt}\n\n"
        f"Also apply these lenses yourself: {also}.\n\n"
        "Judge the diff only against the task. Reply as JSON:\n"
        '{"verdict": "ship"|"needs-work"|"block", "notes": "<2-4 sentences>", '
        '"extra_findings": [{"severity","file","line","summary"}]}\n\n'
        f"{diff}"
    )
    try:
        text, tier = ask(prompt, timeout_s=300.0)
    except NoFrontierAvailable as e:
        return "local-only", "", [], f"frontier review skipped: {e}", ""
    try:
        data = first_object(text)
    except ValueError:
        return "local-only", text[:500], [], "frontier reply was not JSON", tier
    verdict = str(data.get("verdict", "needs-work"))
    notes = str(data.get("notes", "")).strip()
    extra: list[Finding] = []
    raw_extra = data.get("extra_findings", [])
    if isinstance(raw_extra, list):
        for f in raw_extra:
            if isinstance(f, dict):
                extra.append(
                    Finding(
                        lens="frontier",
                        severity=str(f.get("severity", "medium")),
                        file=str(f.get("file", "?")),
                        line=_norm_line(f.get("line", 0)),
                        summary=str(f.get("summary", "")).strip(),
                        verdict="CONFIRMED",
                    )
                )
    return verdict, notes, extra, "", tier


def _wants_frontier(tier: str, diff: str, card: Card) -> bool:
    if tier in ("pro", "max"):
        return True
    # solo: only when a mistake is expensive
    return bool(HIGH_STAKES.search(diff)) or card.tag == "hard"


def review_branch(card: Card, diff: str, *, ledger_has_frontier: bool = True) -> ReviewResult:
    r = ReviewResult()
    if not diff.strip():
        r.verdict, r.skipped = "no-review", "empty diff"
        return r

    files = _files_in_diff(diff)
    selected = pick_lenses(files)
    tier = read_tier()

    if available():
        for name in selected:
            if name in LOCAL_LENSES:
                r.lenses_run.append(name)
                for f in _run_lens_local(name, diff, card):
                    f.verdict = _verify_local(f, diff)
                    if f.verdict != "REFUTED":
                        r.findings.append(f)
    else:
        r.skipped = "local model unreachable; lens pass skipped"

    remaining = [n for n in selected if n not in LOCAL_LENSES]
    if _wants_frontier(tier, diff, card) and ledger_has_frontier:
        verdict, notes, extra, skip, used = _frontier_review(diff, card, r.findings, remaining)
        r.findings.extend(extra)
        r.frontier_notes = notes
        # `used` is the real tier ask() hit -- record it even when the reply was
        # unusable (tokens were still spent, the window consumed).
        if used in ("t3", "t4"):
            r.tier_used = used
        if skip:
            r.skipped = (r.skipped + "; " + skip).strip("; ")
            r.verdict = "local-only" if r.findings or r.lenses_run else "no-review"
        else:
            r.verdict = verdict
            r.tier_used = r.tier_used or "frontier"
    else:
        r.verdict = "local-only" if (r.findings or r.lenses_run) else "no-review"
        if not ledger_has_frontier:
            r.skipped = (r.skipped + "; every frontier tier dry").strip("; ")

    return r


def _files_in_diff(diff: str) -> list[str]:
    out: list[str] = []
    for line in diff.splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", line) or re.match(r"^diff --git a/\S+ b/(.+)$", line)
        if m and m.group(1) != "/dev/null":
            out.append(m.group(1))
    return sorted(set(out))