# SPDX-License-Identifier: Apache-2.0
"""`zecor init` -- one command from a fresh clone to a runnable first task: seed
zecor.config.json, run the doctor checks (optionally fixing them), and -- given
--repo -- verify it actually has a gate and queue one real sample task."""

from __future__ import annotations

import shutil

from zecor import config, doctor, install, repos


def _ensure_config() -> str:
    dst = config.ZECOR_ROOT / "zecor.config.json"
    if dst.exists():
        return f"  ok    zecor.config.json  ({dst})"
    example = config.REPO_ROOT / "zecor.config.example.json"
    if not example.exists():
        return "  MISS  zecor.config.example.json not found -- can't seed a config"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(example, dst)
    return "  made  zecor.config.json  (copied from the example -- edit freely)"


def _ensure_repo(repo: str | None) -> tuple[str, str | None]:
    """(report line, confirmed repo name). None means don't queue a sample card."""
    if repo is None:
        return "  -     no --repo given -- point zecor at one when you're ready", None
    try:
        path = repos.resolve(repo)
    except repos.RepoNotFound as e:
        return f"  MISS  {e}", None
    gate = repos.gate_path(path)
    if gate is None:
        return (
            f"  MISS  {path} has no .zecor/verify.sh -- add one, or run "
            "`zecor scaffold` for a fresh green-gate repo",
            None,
        )
    return f"  ok    {repo} -> {path}  (gate: {gate.relative_to(path)})", repo


def _queue_sample_card(repo: str) -> str:
    config.ensure_dirs()
    config.QUEUE_INBOX.parent.mkdir(parents=True, exist_ok=True)
    line = f"- {repo}: add a one-line usage example to the top of README.md"
    existing = config.QUEUE_INBOX.read_text() if config.QUEUE_INBOX.exists() else ""
    if line in existing:
        return "  ok    sample task already queued in queue/inbox.md"
    with config.QUEUE_INBOX.open("a") as f:
        f.write(line + "\n")
    return "  made  queued a sample task -- `zecor plan && zecor run` to watch it land"


def run(*, repo: str | None, do_install: bool) -> tuple[str, int]:
    lines = ["zecor init", "", _ensure_config()]

    text, gaps = doctor.report()
    lines += ["", text]
    if gaps and do_install:
        itext, _code = install.run(do_it=True)
        lines += ["", itext]

    repo_line, confirmed = _ensure_repo(repo)
    lines += ["", repo_line]
    if confirmed is not None:
        lines.append(_queue_sample_card(confirmed))

    lines.append("")
    if repo is not None and confirmed is None:
        lines.append("fix the repo issue above, then re-run `zecor init --repo ...`")
        return "\n".join(lines), 1
    lines.append("next: `zecor plan` to turn the queue into cards, then `zecor run`.")
    return "\n".join(lines), 0
