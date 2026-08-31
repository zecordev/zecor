# SPDX-License-Identifier: Apache-2.0
"""The verbs -- Command Center build step 2.

Every action Zecor exposes is one function here, called alike by the TUI, `serve.py`,
and the CLI subcommands, so there is exactly one implementation and no second path to
drift. A long job spawns a detached `zecor` subprocess so an SSH drop or a closed TUI
never kills the run.
"""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass, field

from zecor import config
from zecor.cards import Card
from zecor.core import cards as core_cards
from zecor.core.locks import atomic_write_text, file_lock, zecor_bin

RETRYABLE = ("needs-human-design", "blocked", "deferred", "gate-broken")


@dataclass(frozen=True)
class Result:
    ok: bool
    message: str
    data: dict[str, object] = field(default_factory=dict)


# -- control -------------------------------------------------------------------

def stop() -> Result:
    try:
        config.STOP_FILE.write_text("halt via command center\n")
    except OSError as e:
        return Result(False, f"could not touch STOP: {e}")
    _log("stop", {}, {"resulting_stop": True})
    return Result(True, "STOP set -- Zecor halts between tasks")


def go() -> Result:
    with contextlib.suppress(FileNotFoundError):
        config.STOP_FILE.unlink()
    _log("go", {}, {"resulting_stop": False})
    return Result(True, "STOP cleared -- Zecor may run")


def _log(
    verb: str, args: dict[str, object], inverse: dict[str, object] | None
) -> None:
    """Best-effort append to the action log -- a logging failure never blocks a verb."""
    with contextlib.suppress(Exception):
        from zecor.core import actionlog

        actionlog.record(verb, args, inverse)


def is_stopped() -> bool:
    return config.stop_requested()


def repo_names() -> list[str]:
    """Basenames of the repos Zecor knows -- the validated list the chatbot's
    natural-language path must pick from (it may not invent a repo)."""
    from zecor.repos import repo_roots

    return sorted({p.name for p in repo_roots() if p.is_dir()})


# -- cards -------------------------------------------------------------------

def retry(
    card_id: str,
    *,
    tier: str | None = None,
    model: str | None = None,
    note: str | None = None,
) -> Result:
    """Re-queue a stuck card; the precondition is re-checked inside the lock."""

    from zecor.core import leases

    class _Skip(Exception):
        pass

    row = leases.get(card_id)
    if row is not None and leases.is_zombie(row):
        # clear ONLY the row we observed -- if another lane reaped-and-reclaimed in the
        # meantime, its fresh lease is left alone and the check inside _requeue refuses.
        leases.clear_if(card_id, row.token)

    def _requeue(c: Card) -> None:
        if c.state not in RETRYABLE:
            raise _Skip(f"is '{c.state}'")
        live = leases.get(card_id)
        if live is not None and not leases.is_zombie(live):
            raise _Skip("a run currently holds it")
        c.state = "ready"
        c.retry_tier = tier
        c.retry_model = model
        c.retry_context = note
        c.retry_count += 1
        c.defer_count = 0
        c.retry_after = None

    try:
        card = core_cards.mutate(card_id, _requeue)
    except core_cards.CardNotFound:
        return Result(False, f"no card {card_id}")
    except _Skip as e:
        return Result(False, f"{card_id} {e} -- nothing to retry")

    cleared = _clear_deadend(card)
    forced = ""
    if tier:
        forced = f", forcing {tier}" + (f"/{model}" if model else "")
    tail = "; negative-cache cleared" if cleared else ""
    _log("retry", {"id": card_id, "tier": tier, "model": model}, None)  # not reversible
    return Result(
        True,
        f"{card_id} re-queued (retry #{card.retry_count}){forced}{tail}",
        {"retry_count": card.retry_count},
    )


def clear_zombie(card_id: str) -> Result:
    from zecor.core import leases

    leases.clear(card_id)
    _log("clear_zombie", {"id": card_id}, None)  # a fresh lane may already own it
    return Result(True, f"{card_id} lease cleared -- re-dispatch on the next run")


def assign_move(card_id: str, move: str) -> Result:
    """Put a card under a roadmap Move (or clear it with an empty slug). The value is
    slugified so it can actually match a Move id in roadmap/*.md."""
    from zecor.core.roadmap import slugify

    slug = slugify(move) if move.strip() else None
    prior: dict[str, str | None] = {}

    def _fn(c: Card) -> None:
        prior["move"] = c.move
        c.move = slug

    try:
        card = core_cards.mutate(card_id, _fn)
    except core_cards.CardNotFound:
        return Result(False, f"no card {card_id}")
    _log(
        "assign_move",
        {"id": card_id, "move": slug},
        {"card_id": card_id, "prior": prior.get("move"), "base_rev": card.rev},
    )
    return Result(True, f"{card_id} -> move {slug}" if slug else f"{card_id} unassigned")


def _clear_deadend(card: Card) -> bool:
    try:
        from zecor.ledger import Ledger
        from zecor.orchestrator import task_fingerprint

        led = Ledger(config.DB_PATH)
        try:
            return led.clear_deadend(task_fingerprint(card))
        finally:
            led.close()
    except Exception:
        return False


# -- new work -------------------------------------------------------------------

def add_inbox(repo: str, text: str) -> Result:
    """Append one `- <repo>: <text>` line to queue/inbox.md, serialized and atomic so
    concurrent callers cannot lose each other's line. This is the only thing the
    chatbot's natural-language path may write -- `zecor plan` turns it into a scoped
    card."""
    repo = repo.strip()
    text = " ".join(text.split())
    if not repo or not text:
        return Result(False, "need both a repo and a description")
    line = f"- {repo}: {text}"
    path = config.QUEUE_INBOX
    try:
        with file_lock(path.parent / ".inbox.lock"):
            existing = path.read_text() if path.exists() else ""
            body = existing if (not existing or existing.endswith("\n")) else existing + "\n"
            atomic_write_text(path, body + line + "\n")
    except OSError as e:
        return Result(False, f"could not write the inbox: {e}")
    _log("add_inbox", {"line": line}, {"line": line})
    return Result(True, f"queued for planning: {line}", {"line": line})


def comment_planned(done: set[str]) -> None:
    """Prefix `# planned: ` onto every inbox line whose exact text is in `done`. Taken
    under the inbox lock and written atomically so a concurrent `add_inbox` is not lost."""
    if not done:
        return
    path = config.QUEUE_INBOX
    with file_lock(path.parent / ".inbox.lock"):
        if not path.exists():
            return
        lines = path.read_text().splitlines()
        rewritten = "\n".join(
            (f"# planned: {ln}" if ln in done else ln) for ln in lines
        )
        atomic_write_text(path, rewritten + "\n")


# -- feature fan-out ----------------------------------------------------------

def fanout(requirement: str, repos: list[str], *, move: str | None = None) -> Result:
    """One requirement, one card in each client repo, tagged with a shared `fanout` id
    so `fanout_status` can check the parallel implementations stay in step."""
    import secrets

    from zecor.cards import InboxItem
    from zecor.planner import plan_item

    text = " ".join(requirement.split())
    seen = repo_names()
    bad = [r for r in repos if seen and r not in seen]
    if bad:
        return Result(False, f"unknown repo(s): {', '.join(bad)}")
    if len(repos) < 2:
        return Result(False, "fan-out needs at least two repos")

    fid = "fo-" + secrets.token_hex(3)
    made: list[str] = []
    made_ids: list[str] = []
    for repo in repos:
        cid = f"{core_cards.next_id():04d}"  # re-read per card -- our own creates count
        card = plan_item(InboxItem(repo, text, f"- {repo}: {text}"), cid)
        card.fanout = fid
        if move:
            card.move = move
        try:
            core_cards.create(card)
        except core_cards.CardExists:
            # a concurrent planner took the id -- a partial fan-out is worse than none.
            for done in made_ids:
                with contextlib.suppress(OSError):
                    (config.TASKS_DIR / f"{done}.md").unlink()
            return Result(False, f"id collision on {cid} -- rolled back, retry the fan-out")
        made.append(f"{cid} {repo}")
        made_ids.append(cid)
    _log("fanout", {"fanout": fid, "repos": repos}, None)
    return Result(
        True,
        f"fan-out {fid}: {len(made)} cards -- {', '.join(made)}",
        {"fanout": fid, "cards": made},
    )


def fanout_status(fid: str) -> Result:
    """Where each card of a fan-out group stands, and whether they've diverged."""
    from zecor.core import view

    rows = [c for c in view.cards() if c.fanout == fid]
    if not rows:
        return Result(False, f"no cards tagged {fid}")
    states = {c.state for c in rows}
    lines = [f"  {c.id} {c.repo:20} {c.state}" for c in sorted(rows, key=lambda c: c.repo)]
    diverged = len(states) > 1 and "landed" in states
    head = (
        f"fan-out {fid}: {len(rows)} cards, states {sorted(states)}"
        + ("  -- DIVERGED (some landed, some not)" if diverged else "")
    )
    return Result(not diverged, head + "\n" + "\n".join(lines), {"diverged": diverged})


# -- long jobs (detached) ------------------------------------------------------

def run(*, repo: str | None = None, max_n: int | None = None, until: str | None = None) -> Result:
    argv = ["run"]
    if max_n is not None:
        argv += ["--max", str(max_n)]
    if until:
        argv += ["--until", until]
    # `repo` filter isn't a `zecor run` flag yet; accepted here so the intent is stable.
    return _spawn(argv, "zecor run", extra={"repo": repo} if repo else None)


def plan() -> Result:
    return _spawn(["plan"], "zecor plan")




def _spawn(argv: list[str], label: str, *, extra: dict[str, object] | None = None) -> Result:
    log = config.LOGS_DIR / f"cc-{argv[0]}.log"
    try:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        fh = log.open("a")
    except OSError as e:
        return Result(False, f"could not open {log}: {e}")
    try:
        proc = subprocess.Popen(
            [*zecor_bin(), *argv],
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # outlives an SSH drop or a closed TUI
            cwd=str(config.REPO_ROOT),
        )
    except OSError as e:
        return Result(False, f"could not start {label}: {e}")
    finally:
        fh.close()  # the child dup'd the fd; the parent handle would otherwise leak
    data: dict[str, object] = {"pid": proc.pid, "log": str(log)}
    if extra:
        data.update(extra)
    return Result(True, f"{label} started (pid {proc.pid}) -> {log}", data)