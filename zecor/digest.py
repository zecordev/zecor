# SPDX-License-Identifier: Apache-2.0
"""The digest.

Generated locally at 07:00, zero frontier tokens. The headline metric is frontier
tokens per landed PR -- if it is not trending down, the system is not learning.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from zecor.orchestrator import TaskOutcome


def render(outcomes: list[TaskOutcome], db_path: Path, *, day: date | None = None) -> str:
    day = day or datetime.now(UTC).date()
    landed = [o for o in outcomes if o.state == "landed"]
    blocked = [o for o in outcomes if o.state == "blocked"]
    deferred = [o for o in outcomes if o.state == "deferred"]
    human = [o for o in outcomes if o.state == "needs-human-design"]
    broken = [o for o in outcomes if o.state == "gate-broken"]

    lines: list[str] = [
        f"# {day.isoformat()} - {len(outcomes)} tasks, {len(landed)} landed, "
        f"{len(blocked)} blocked, {len(deferred)} deferred",
        "",
    ]

    if landed:
        lines += ["## Landed (review these)"]
        for o in landed:
            tiers = " -> ".join(o.tiers_used) or "-"
            ft = o.frontier_in_tokens + o.frontier_out_tokens
            where = o.pr_url or o.lane_branch or "?"
            lines.append(
                f"- {o.task_id}  {tiers}, {len(o.attempts)} attempt(s), "
                f"{ft} frontier tokens  -> {where}"
            )
            if o.review is not None:
                v = o.review
                hi = [f for f in v.findings if f.severity == "high"]
                lines.append(f"    {v.summary_line()}")
                for f in hi:
                    lines.append(f"      ! {f.file}:{f.line} ({f.lens}) {f.summary}")
        lines.append("")

    if human or broken:
        lines += ["## Needs you"]
        for o in human:
            lines.append(f"- {o.task_id}  needs design -- {o.note}")
        for o in broken:
            lines.append(f"- {o.task_id}  gate misconfigured -- {o.note}")
        lines.append("")

    if blocked:
        lines += ["## Blocked"]
        for o in blocked:
            q = o.brief.question_for_escalation if o.brief else None
            lines.append(f"- {o.task_id}  {o.note}")
            if q:
                lines.append(f"  Open question: {q}")
            if o.lane_branch:
                lines.append(f"  Best attempt: {o.lane_branch}")
        lines.append("")

    if deferred:
        lines += ["## Deferred (queue survived; retry after the hold)"]
        for o in deferred:
            lines.append(f"- {o.task_id}  {o.note}")
        lines.append("")

    lines += _spend_block(db_path, outcomes)
    return "\n".join(lines) + "\n"


def _spend_block(db_path: Path, outcomes: list[TaskOutcome]) -> list[str]:
    lines = ["## Spend"]
    if db_path.exists():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT vendor, COUNT(*) n, COALESCE(SUM(in_tokens+out_tokens),0) tok "
            "FROM attempt GROUP BY vendor ORDER BY vendor"
        ).fetchall()
        for r in rows:
            vendor = str(r["vendor"] or "local")
            lines.append(f"  {vendor:16s} {int(r['n']):4d} calls   {int(r['tok'] or 0):>9d} tokens")
        db.close()
    landed = [o for o in outcomes if o.state == "landed"]
    total_ft = sum(o.frontier_in_tokens + o.frontier_out_tokens for o in landed)
    per_pr = total_ft // len(landed) if landed else 0
    lines.append(f"  Frontier tokens per landed PR: {per_pr}")
    lines.append("  (trending down means the system is learning)")
    return lines


def write(outcomes: list[TaskOutcome], db_path: Path, digest_dir: Path) -> Path:
    digest_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).date()
    path = digest_dir / f"{day.isoformat()}.md"
    body = render(outcomes, db_path, day=day) if outcomes else render_from_ledger(db_path, day)
    path.write_text(body)
    return path


def render_from_ledger(db_path: Path, day: date) -> str:
    """Rebuild the day's picture from the attempt table -- for `zecor digest` when there
    are no in-memory outcomes (e.g. re-rendering after the fact)."""
    lines = [f"# {day.isoformat()} - from the ledger", ""]
    if not db_path.exists():
        return "\n".join([*lines, "(no ledger)", ""]) + "\n"
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    start = day.isoformat()
    rows = db.execute(
        "SELECT task_id, tier, model, verify_exit, outcome, reason, in_tokens, out_tokens, "
        "started_at FROM attempt WHERE started_at >= ? ORDER BY started_at",
        (start,),
    ).fetchall()
    by_task: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)
    landed = [t for t, rs in by_task.items() if rs[-1]["outcome"] == "pass"]
    lines.append(f"{len(by_task)} task(s) touched, {len(landed)} reached a green gate")
    lines.append("")
    for t, rs in by_task.items():
        last = rs[-1]
        path = " -> ".join(dict.fromkeys(f"{x['tier']}" for x in rs))
        ft = sum(int(x["in_tokens"] or 0) + int(x["out_tokens"] or 0) for x in rs)
        lines.append(f"- {t}  {last['outcome']}  ({len(rs)} attempt(s), tiers {path}, {ft} tokens)")
    lines += _spend_block(db_path, [])
    db.close()
    return "\n".join(lines) + "\n"