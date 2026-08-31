# SPDX-License-Identifier: Apache-2.0
"""The Zecor CLI -- the only entry point.

zecor plan            expand queue/inbox.md into task cards
zecor run [--until HH:MM] [--max N]
zecor digest          re-render today's digest from the ledger
zecor status          quota state, queue depth, STOP flag
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from zecor import config, digest
from zecor.cards import Card, parse_inbox
from zecor.decompose import decompose, to_inbox_block
from zecor.frontier import NoFrontierAvailable
from zecor.ledger import Ledger
from zecor.orchestrator import TaskOutcome, run_task
from zecor.planner import plan_item, review_queue
from zecor.repos import RepoNotFound, resolve
from zecor.resources import advise_lanes, can_start, snapshot


def _next_id_int(tasks_dir: Path) -> int:
    existing = sorted(int(p.stem) for p in tasks_dir.glob("[0-9]*.md") if p.stem.isdigit())
    return (existing[-1] + 1) if existing else 1


def _next_id(tasks_dir: Path) -> str:
    return f"{_next_id_int(tasks_dir):04d}"


def cmd_plan(_: argparse.Namespace) -> int:
    config.ensure_dirs()
    if not config.QUEUE_INBOX.exists():
        print("no queue/inbox.md")
        return 1
    items = parse_inbox(config.QUEUE_INBOX.read_text())
    if not items:
        print("inbox is empty")
        return 0
    cards: list[Card] = []
    raw_by_id: dict[str, str] = {}
    for i, item in enumerate(items):
        card = plan_item(item, f"{_next_id_int(config.TASKS_DIR) + i:04d}")
        cards.append(card)
        raw_by_id[card.id] = item.raw
    cards, review_note = review_queue(cards)
    from zecor.core import cards as core_cards

    made: list[str] = []
    done: set[str] = set()  # only inbox lines whose card actually landed get commented
    for card in cards:
        try:
            core_cards.create(card)
        except core_cards.CardExists:
            print(f"  skip {card.id}: a card with that id already exists")
            continue
        made.append(f"{card.id}  [{card.klass.value}/{card.state}]  {card.repo}: {card.intent}")
        if card.id in raw_by_id:
            done.add(raw_by_id[card.id])
    if review_note:
        print(review_note)
    from zecor.core import intents

    intents.comment_planned(done)
    print(f"planned {len(made)} task(s):")
    for m in made:
        print(f"  {m}")
    return 0


def cmd_run(ns: argparse.Namespace) -> int:
    config.ensure_dirs()
    # One `zecor run` per state tree -- a launchd double-fire or a manual run overlapping
    # the nightly one would each start lanes with no shared reservation.
    lock_path = config.ZECOR_ROOT / ".run.lock"
    run_lock = open(lock_path, "w")  # noqa: SIM115 -- held for the whole run
    try:
        import fcntl

        fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError):
        print(f"another `zecor run` holds {lock_path} -- exiting")
        run_lock.close()
        return 0
    try:
        return _run_queue(ns)
    finally:
        run_lock.close()


def _preflight() -> tuple[list[str], list[str]]:
    """(blockers, warnings). Blockers stop the run -- the local model is the base tier
    for briefs/lenses/intake, and a thrashing/full disk means nothing good happens."""
    from zecor.localmodel import available

    s = snapshot()
    blockers: list[str] = []
    warns: list[str] = []
    if not available():
        blockers.append("local model not reachable -- `lms server start` / load a model")
    if s.swap_used_gb > 6.0:
        blockers.append(f"swap {s.swap_used_gb:.1f}GB used -- machine is thrashing, let it settle")
    if _disk_free_gb() < config.DISK_FLOOR_GB:
        blockers.append(f"disk below {config.DISK_FLOOR_GB}GB floor")
    if s.local_consumers:
        warns.append(f"{s.local_consumers} local-model job(s) already running -- lanes will queue")
    if s.probe_errors:
        warns.append(f"resource probes degraded: {'; '.join(s.probe_errors)}")
    return blockers, warns


# task ids this `zecor run` invocation acquired a lease for -- so the shutdown handler
# clears exactly those, not "any lease with our pid" (pids get reused).
_MY_LEASES: set[str] = set()
_SHUTTING_DOWN = False


def note_lease(task_id: str) -> None:
    _MY_LEASES.add(task_id)


def _shutdown(signum: int, _frame: object = None) -> None:
    """Worktrees and `ready` cards are already crash-safe -- this just keeps a clean
    shutdown from leaving a lease that would need zombie-reaping."""
    global _SHUTTING_DOWN
    if _SHUTTING_DOWN:  # a second signal mid-cleanup must not re-enter
        raise SystemExit(128 + signum)
    _SHUTTING_DOWN = True
    import signal

    for s in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(s, signal.SIG_IGN)

    from zecor.core import leases

    with contextlib.suppress(Exception):
        for tid in list(_MY_LEASES):
            leases.clear(tid)
    with contextlib.suppress(OSError):
        config.STOP_FILE.write_text(f"halted by signal {signum}\n")
    raise SystemExit(128 + signum)


def _install_shutdown_handler() -> None:
    """Wire SIGTERM/SIGINT (OS shutdown, launchd unload, kill) to `_shutdown`."""
    import signal

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):  # not the main thread -> skip
            signal.signal(sig, _shutdown)


def _run_queue(ns: argparse.Namespace) -> int:
    import threading
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor

    _install_shutdown_handler()
    until = _parse_until(ns.until)

    blockers, warns = _preflight()
    for w in warns:
        print(f"preflight warn: {w}")
    for b in blockers:
        print(f"preflight BLOCK: {b}")
    if blockers and not ns.force:
        print("run aborted -- fix the above or pass --force")
        return 1
    if blockers:
        print("--force: proceeding past the blockers above")

    cards = _ready_cards()
    if not cards:
        print("no ready task cards -- run `zecor plan` first")
        return 0

    # Resolve repos and apply the wall-clock / disk gates up front; STOP is re-checked
    # before each submission (tasks already in flight are allowed to finish).
    eligible: list[tuple[Card, Path]] = []
    for card in cards:
        if ns.max and len(eligible) >= ns.max:
            break
        try:
            eligible.append((card, resolve(card.repo)))
        except RepoNotFound as e:
            print(f"{card.id}: {e}")

    git_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
    frontier_lock = threading.Lock()  # one paid-tier call at a time -> no cap race
    outcomes: list[TaskOutcome] = []
    # Lane count is capped by config but throttled by live RAM/load -- a second lane on
    # a box already 32GB into LM Studio thrashes the GPU.
    advised = advise_lanes()
    workers = max(1, min(config.MAX_CONCURRENT_LANES, advised))
    if workers < config.MAX_CONCURRENT_LANES:
        print(f"resource governor: {workers} lane(s) (config allows {config.MAX_CONCURRENT_LANES})")

    # Create the db + set WAL once, single-threaded; each lane then opens its own
    # connection to the already-WAL file (no mode change, no lock contention).
    Ledger.open(config.DB_PATH, config.SCHEMA_PATH).close()

    def work(card: Card, repo_dir: Path) -> tuple[TaskOutcome, Card]:
        """Run one card and persist its outcome while the dispatch lease is still held,
        so no second `zecor run` can claim the card in the gap before it is marked.
        Returns the untouched card when another lane stole the lease mid-run -- then
        nothing must be stamped."""
        from zecor.core import leases

        try:
            with leases.lease(card.id) as lost:
                note_lease(card.id)  # so the shutdown handler releases exactly this one
                ledger = Ledger(config.DB_PATH)
                out = run_task(
                    card,
                    ledger,
                    repo_root=repo_dir,
                    git_lock=git_locks[str(repo_dir)],
                    frontier_lock=frontier_lock,
                )
                if lost():  # another lane owns this card now -- leave its state alone
                    out.state = "deferred"
                    out.note = "lease reaped mid-run (suspend?); the other lane owns it"
                    return out, card
                return out, _mark_card(card, out.state)  # persisted under the lease
        except leases.Leased:
            return (
                TaskOutcome(card.id, "deferred", note="another run holds this card's lease"),
                card,
            )
        except Exception as e:  # one bad task must not kill the run
            return TaskOutcome(card.id, "blocked", note=f"harness error: {e}"), card

    def _halt_reason() -> str | None:
        if config.stop_requested():
            return "STOP file present"
        if until is not None and datetime.now().astimezone() >= until:
            return f"reached --until {ns.until} (local)"
        if _disk_free_gb() < config.DISK_FLOOR_GB:
            return f"disk below {config.DISK_FLOOR_GB}GB floor"
        return None

    # Submit in waves of `workers`, re-checking the gates between waves -- a single
    # submit-everything loop would ignore `--until` and a phone-touched STOP file for
    # the rest of a slow night.
    print(f"running {len(eligible)} task(s), up to {workers} at once")
    with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_LANES) as pool:
        pending = list(eligible)
        while pending:
            halt = _halt_reason()
            if halt:
                print(f"{halt} -- stopping ({len(pending)} task(s) left in queue)")
                break
            # Re-check the governor each wave -- RAM/load drifts over a long run.
            wave_n = max(1, min(config.MAX_CONCURRENT_LANES, advise_lanes()))
            wave, pending = pending[:wave_n], pending[wave_n:]
            futures = {pool.submit(work, c, rd): c for c, rd in wave}
            for fut in futures:
                outcome, card = fut.result()
                if outcome.state == "deferred":
                    if card.state == "needs-human-design":
                        outcome.state = "needs-human-design"
                        outcome.note += (
                            f"  (deferred {card.defer_count}x, never routed -- needs you)"
                        )
                    elif card.retry_after:
                        outcome.note += f"  (holding until {card.retry_after})"
                outcomes.append(outcome)
                print(f"   {card.id} {outcome.state}: {outcome.note}")

    path = digest.write(outcomes, config.DB_PATH, config.DIGEST_DIR)
    print(f"\ndigest: {path}")
    return 0


def cmd_decompose(ns: argparse.Namespace) -> int:
    config.ensure_dirs()
    doc = Path(ns.doc)
    if not doc.is_absolute():
        doc = (
            config.PROJECTS_DIR / doc if (config.PROJECTS_DIR / doc).exists() else Path.cwd() / doc
        )
    if not doc.exists():
        print(f"no such file: {doc}")
        return 1
    print(f"decomposing {doc.name} ... (one frontier call, may take a minute)")
    try:
        d = decompose(doc.read_text())
    except NoFrontierAvailable as e:
        print(f"cannot decompose: {e}")
        return 1
    block = to_inbox_block(d, doc.name)
    config.QUEUE_INBOX.parent.mkdir(parents=True, exist_ok=True)
    with config.QUEUE_INBOX.open("a") as fh:
        fh.write(block)
    ready = sum(1 for t in d.tasks if t.get("ready", True))
    print(
        f"proposed {len(d.tasks)} task(s) ({ready} ready) for repo '{d.repo}' via {d.tier}, "
        f"{len(d.open_questions)} open question(s)."
    )
    print(f"review and uncomment in {config.QUEUE_INBOX}, then `zecor plan`.")
    return 0


def cmd_digest(_: argparse.Namespace) -> int:
    config.ensure_dirs()
    path = digest.write([], config.DB_PATH, config.DIGEST_DIR)
    print(path.read_text())
    return 0


def cmd_retry(ns: argparse.Namespace) -> int:
    """Re-queue a needs-human / blocked / deferred card, optionally forcing the first
    attempt's tier and model, carrying a note into the loop as a constraint, and
    clearing the negative-cache entry so it isn't skipped."""
    from zecor.core import intents

    config.ensure_dirs()
    res = intents.retry(ns.id, tier=ns.tier, model=ns.model, note=ns.note)
    print(res.message + (" -- next `zecor run` picks it up" if res.ok else ""))
    return 0 if res.ok else 1


def cmd_stop(_: argparse.Namespace) -> int:
    from zecor.core import intents

    print(intents.stop().message)
    return 0


def cmd_go(_: argparse.Namespace) -> int:
    from zecor.core import intents

    print(intents.go().message)
    return 0


def cmd_ask(ns: argparse.Namespace) -> int:
    """One-shot prompt at one tier -- poke a model without the workflow."""
    from pathlib import Path as _P

    prompt = ns.prompt or sys.stdin.read()
    if not prompt.strip():
        print("nothing to ask (give a prompt or pipe one on stdin)")
        return 1

    if ns.tier in ("local", "t1"):
        from zecor.config import MODEL_PRIMARY_CODER
        from zecor.localmodel import LocalModelError, available, complete

        if not available():
            print("local model not reachable -- `lms server start --port 1234`")
            return 1
        try:
            out = complete(prompt, model=ns.model or MODEL_PRIMARY_CODER, max_tokens=ns.max_tokens)
        except LocalModelError as e:
            print(f"local model error: {e}")
            return 1
        print(out.text)
        print(f"\n[{ns.model or MODEL_PRIMARY_CODER}  in {out.in_tokens} / out {out.out_tokens}]")
        return 0

    if ns.tier in ("gemini", "t2a"):
        from zecor.workers.base import Job
        from zecor.workers.gemini import GeminiWorker

        r = GeminiWorker().run(Job(prompt, _P.cwd(), ns.model, float(ns.timeout), "true"))
        print(r.stdout or f"[{r.outcome}] {r.stderr}")
        return 0 if r.ok else 1

    if ns.tier in ("claude", "codex", "frontier"):
        from zecor.frontier import NoFrontierAvailable, ask

        try:
            text, which = ask(prompt, timeout_s=float(ns.timeout))
        except NoFrontierAvailable as e:
            print(str(e))
            return 1
        print(text)
        print(f"\n[{which}]")
        return 0

    print(f"unknown tier {ns.tier!r} (local | gemini | claude)")
    return 1


def cmd_serve(ns: argparse.Namespace) -> int:
    from zecor.serve import serve

    return serve(ns.bind, ns.port, ns.token)


def cmd_inventory(ns: argparse.Namespace) -> int:
    """Architecture legwork -- mechanical inventory, no model, no judgment."""
    from zecor import inventory

    config.ensure_dirs()
    try:
        repo_dir = resolve(ns.repo)
    except RepoNotFound as e:
        print(e)
        return 1
    path = inventory.write(repo_dir, ns.topic or ns.repo, config.DIGEST_DIR)
    print(f"wrote {path}")
    print(path.read_text())
    return 0


def cmd_scaffold(ns: argparse.Namespace) -> int:
    """Lay down a from-scratch project with a green gate, so Zecor has something to
    fail -- then add `- <name>: <first feature>` to the inbox and run normally."""
    from zecor.greenfield import scaffold

    dest = Path(ns.dest).expanduser().resolve() if ns.dest else Path.cwd()
    res = scaffold(ns.name, ns.description, dest, kind=ns.kind)
    print(res.note)
    if not res.files:
        return 1
    for f in res.files:
        print(f"  {f}")
    mark = "gate green" if res.gate_verified else "gate NOT verified here"
    print(f"\n{mark} -- bash {res.path}/.zecor/verify.sh")
    return 0


def cmd_deploy(ns: argparse.Namespace) -> int:
    """Manual web deploy: build <repo> at <sha> and roll it out to Cloud Run (needs
    ZECOR_GCP_PROJECT + gcloud). The auto path (SHIP-IT.md step 4) is not wired yet."""
    from zecor import deploy

    if not deploy.configured():
        print("GCP not configured -- set ZECOR_GCP_PROJECT and install gcloud")
        return 1
    r = deploy.deploy(ns.repo, ns.sha)
    print(r.note + (f" -- {r.revision} {r.url}".rstrip() if r.ok else ""))
    return 0 if r.ok else 1


def cmd_deploys(ns: argparse.Namespace) -> int:
    """Held deploys awaiting a human ack, or approve/reject one."""
    from zecor import deploy

    config.ensure_dirs()
    if ns.approve is not None:
        r = deploy.approve_pending(ns.approve)
        print(r.note)
        return 0 if r.ok else 1
    if ns.reject is not None:
        ok = deploy.reject_pending(ns.reject)
        print("rejected" if ok else "no such pending deploy")
        return 0 if ok else 1
    pend = deploy.list_pending()
    if not pend:
        print("no held deploys")
        return 0
    for p in pend:
        print(f"  #{p.id}  {p.repo:20} {p.sha[:12]}  requested {p.requested_at[:19]}")
    return 0


def cmd_init(ns: argparse.Namespace) -> int:
    """One command from a fresh clone to a runnable first task."""
    from zecor import onboarding

    text, code = onboarding.run(repo=ns.repo, do_install=ns.yes)
    print(text)
    return code


def cmd_install(ns: argparse.Namespace) -> int:
    """Act on what `zecor doctor` flags -- dry-run by default, --yes executes the
    non-interactive steps."""
    from zecor import install

    text, code = install.run(do_it=ns.yes)
    print(text)
    return code


def cmd_remote(ns: argparse.Namespace) -> int:
    """Set up the phone path: Tailscale + mosh + tmux, then print the connection string
    (+ QR). Interactive runs offer the brew installs; `-y`/`--install` skips the prompt,
    a non-tty just prints the commands."""
    import subprocess
    import sys as _sys

    from zecor import remote

    if ns.sub == "string":
        st = remote.status()
        print(remote.connection_string(st.dns_name or st.ipv4 or "<host>"))
        return 0

    assume_yes = ns.install or getattr(ns, "yes", False)
    interactive = _sys.stdin.isatty()

    def _ok(question: str) -> bool:
        if assume_yes:
            return True
        if not interactive:
            return False
        return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")

    st = remote.status()
    missing = [p for p, ok in (("tailscale", st.tailscale), ("mosh", st.mosh),
                               ("tmux", st.tmux)) if not ok]
    if missing and shutil.which("brew") and _ok(f"Install {', '.join(missing)} with Homebrew now?"):
        if subprocess.run(["brew", "install", *missing], check=False).returncode != 0:
            print("brew install failed -- fix the above and re-run `zecor remote setup`")
            return 1
        st = remote.status()

    bring_up_q = "Bring Tailscale up now (starts the daemon, opens a sign-in)?"
    if st.tailscale and not st.up and _ok(bring_up_q):
        probe = subprocess.run(["tailscale", "status"], capture_output=True, text=True, check=False)
        if "failed to connect" in (probe.stdout + probe.stderr).lower() and shutil.which("brew"):
            print("  starting the tailscale daemon (needs sudo)...")
            subprocess.run(["sudo", "brew", "services", "start", "tailscale"], check=False)
        print("  tailscale up -- finish the browser sign-in it opens...")
        subprocess.run(["sudo", "tailscale", "up"], check=False)
        st = remote.status()

    text, code = remote.plan(do_install=False)
    print(text)
    return code


def cmd_git(ns: argparse.Namespace) -> int:
    """In-process git queries via `zecor-gitengine` (gitoxide), with a `git` fallback.
    `branch-state <branch> --base <base>` is the check the auto-merge gate needs."""
    import json as _json

    from zecor import gitengine

    repo = Path(ns.repo)
    if ns.sub == "branch-state":
        s = gitengine.branch_state(repo, ns.branch, ns.base)
        if s is None:
            print("could not read branch state")
            return 1
        print(_json.dumps(s.__dict__, indent=2))
        return 0
    if ns.sub == "changed-files":
        for f in gitengine.changed_files(repo, ns.a, ns.b):
            print(f)
        return 0
    if ns.sub == "rev-parse":
        sha = gitengine.rev_parse(repo, ns.rev)
        print(sha or "")
        return 0 if sha else 1
    if ns.sub == "log":
        for le in gitengine.log(repo, ns.rev, ns.limit):
            print(f"{le.sha[:12]}  {le.summary}  ({le.author})")
        return 0
    if ns.sub == "status":
        st = gitengine.status(repo)
        if st is None:
            print("could not read status")
            return 1
        if st.is_clean:
            print("clean")
            return 0
        for kind, rows in (("staged  ", st.staged), ("unstaged", st.unstaged)):
            for se in rows:
                print(f"  {kind}  {se.change:12} {se.path}")
        for p in st.untracked:
            print(f"  untracked            {p}")
        return 0
    return 2


def cmd_mode(_: argparse.Namespace) -> int:
    """Print the active security posture and what it allows -- set via ZECOR_MODE or the
    `mode` key in zecor.config.json."""
    from zecor import modes

    d = modes.as_dict()
    print(f"mode: {d['mode']}")
    for k in ("automerge_allowed", "automerge_requires_clean_sweep", "draft_pr_only",
              "shell_allow_listed", "external_input_hostile"):
        print(f"  {k:32} {d[k]}")
    return 0


def cmd_scan(ns: argparse.Namespace) -> int:
    """Scan a file, stdin, `git diff`, or a directory tree for secrets. `--sarif` emits
    SARIF 2.1.0 for CI / GitHub code scanning; exit 1 on a hit."""
    import json as _json
    import sys as _sys

    from zecor import secretscan

    if ns.dir:
        findings = secretscan.scan_tree(ns.dir, injection=ns.injection,
                                        respect_gitignore=not ns.no_gitignore)
    elif ns.diff:
        import subprocess

        text = subprocess.run(["git", "diff", "--cached" if ns.cached else "HEAD"],
                              capture_output=True, text=True, check=False).stdout
        findings = secretscan.scan_diff(text)
    elif ns.path:
        findings = secretscan.scan_text(Path(ns.path).read_text(), file=ns.path)
    else:
        findings = secretscan.scan_text(_sys.stdin.read())

    if ns.sarif:
        rules = sorted({f.rule for f in findings})
        print(_json.dumps({
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [{
                "tool": {"driver": {"name": "zecor-secretscan", "informationUri": "https://zecor.dev",
                                    "rules": [{"id": r} for r in rules]}},
                "results": [{
                    "ruleId": f.rule, "level": "error",
                    "message": {"text": f"{f.rule} (masked: {f.hint})"},
                    "partialFingerprints": {"zecor/v1": secretscan.fingerprint(f)},
                    "locations": [{"physicalLocation": {
                        "artifactLocation": {"uri": f.file},
                        "region": {"startLine": max(f.line, 1)}}}],
                } for f in findings],
            }],
        }, indent=2))
        return 1 if findings else 0

    for f in findings:
        print(f"  {f.rule:22} {f.file}:{f.line}  {f.hint}")
    print("clean" if not findings else f"{len(findings)} finding(s)")
    return 0 if not findings else 1


def cmd_sandbox(ns: argparse.Namespace) -> int:
    """Run a command under `zecor-sandbox` (OS-level isolation), or `show` the policy the
    current mode would enforce. The engine does this automatically for a lane's gate in
    an isolation mode; this is for inspecting or reproducing it by hand."""
    import subprocess

    from zecor import _rustbridge, sandbox

    b = _rustbridge.binary("zecor-sandbox")
    if b is None:
        print("zecor-sandbox not found -- install from github.com/zecordev/zecor-sandbox "
              "(prebuilt binary into ./bin, or `cargo install --git ...`)")
        return 1
    workdir = Path(ns.workdir).resolve()
    if ns.sub == "show":
        sandbox.wrap(["true"], workdir=workdir, allow_net=ns.allow_net, force=True)
        pol = workdir / ".zecor-sandbox.policy"
        return subprocess.run([b, "show", "--policy", str(pol)], check=False).returncode
    cmd = list(ns.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("nothing to run -- pass `-- CMD [ARGS...]`")
        return 2
    argv = sandbox.wrap(cmd, workdir=workdir, allow_net=ns.allow_net, force=True)
    if argv[:1] == [b]:  # sandbox engaged -- splice in the pass-through flags
        extra = []
        if ns.dry_run:
            extra.append("--dry-run")
        if ns.audit:
            extra += ["--audit", str(Path(ns.audit).resolve())]
        argv = [argv[0], argv[1], *extra, *argv[2:]]
    return subprocess.run(argv, cwd=str(workdir), check=False).returncode


def cmd_doctor(_: argparse.Namespace) -> int:
    """One report of every dependency with the fix for each gap -- spends no tokens."""
    from zecor import doctor

    text, gaps = doctor.report()
    print(text)
    return 0 if gaps == 0 else 1


def cmd_supervise(ns: argparse.Namespace) -> int:
    """The keep-alive loop: run `zecor run` in bounded slices, replenish the queue,
    run the watchers, and do the daily progress pass -- honours STOP and the spend
    ceiling. `--status` reads the heartbeat; `--once` runs one iteration."""
    config.ensure_dirs()
    from zecor import supervise

    if ns.status:
        hb = supervise.read_heartbeat()
        if not hb:
            print("supervisor: not running (no heartbeat)")
            return 1
        at = str(hb.get("at", "?"))[:19]
        print(f"supervisor pid {hb.get('pid')}  last tick {at}  "
              f"ceiling={hb.get('ceiling')}  backoff={hb.get('backoff_s', 0)}s  "
              f"stopped={hb.get('stopped')}")
        return 0
    return supervise.supervise_loop(run_hours=ns.run_hours, once=ns.once)


def cmd_watchd(ns: argparse.Namespace) -> int:
    """Run just the always-on watchers (questions / zombie notices / attempt narration)
    into the transcript -- so events are captured with no console attached."""
    config.ensure_dirs()
    import time

    from zecor import supervise

    state = None
    while True:
        state = supervise.watch_tick(state)
        if ns.once:
            return 0
        time.sleep(ns.interval)


def cmd_cost(ns: argparse.Namespace) -> int:
    """Price a chunk of context: exact BPE count (zecor-tokcount) x a model's list
    price. Reads a file if given, else stdin; `--out-tokens` adds an expected
    completion."""
    import sys as _sys

    from zecor import tokens

    text = Path(ns.path).read_text() if ns.path else _sys.stdin.read()
    n = tokens.count(text, encoding=ns.enc)
    c = tokens.cost(ns.model, n, ns.out_tokens)
    print(f"model        {c['model']}")
    print(f"input        {c['in_tokens']:>10,} tok   ${float(c['in_usd']):.4f}")
    if ns.out_tokens:
        print(f"output(est)  {c['out_tokens']:>10,} tok   ${float(c['out_usd']):.4f}")
    print(f"total                        ${float(c['total_usd']):.4f}")
    return 0


def cmd_progress(_: argparse.Namespace) -> int:
    """Run the daily progress pass: roll the roadmap up, log what landed / started /
    got stuck since last time, flag Moves going nowhere."""
    config.ensure_dirs()
    from zecor.core import progress

    rep = progress.run_once()
    if not rep.events:
        print("no roadmap movement since the last pass")
        return 0
    for e in rep.events:
        print(f"  {e.kind:8}  {e.slug:22}  {e.title}" + (f"  -- {e.detail}" if e.detail else ""))
    if rep.log_path:
        print(f"logged -> {rep.log_path}")
    return 0


def cmd_budget(_: argparse.Namespace) -> int:
    """The unattended spend ceiling: ok / throttle (local-only) / pause."""
    from zecor.core import budget

    st = budget.ceiling_state()
    print(f"{st.state.upper()}: {st.reason}")
    return 0 if st.state != "pause" else 2


def cmd_metrics(_: argparse.Namespace) -> int:
    """Land rate by tier, cycle time, rough cost, daily-lands sparkline from the attempt
    ledger -- says so plainly while the ledger is empty."""
    from zecor.core import metrics

    s = metrics.summary()
    if s.empty:
        print("no runs yet -- the attempt ledger is empty")
        return 0
    print(f"attempts {s.total_attempts}   lands {s.total_lands}   "
          f"est cost ${s.est_cost_usd:.2f}")
    if s.median_cycle_s is not None:
        print(f"cycle time: median {s.median_cycle_s:.0f}s   p90 {s.p90_cycle_s:.0f}s")
    for t in s.by_tier:
        print(f"  {t.tier:5}  {t.lands:3}/{t.attempts:<3}  {t.rate:5.0%}")
    print(f"daily lands: {metrics.sparkline([float(x) for x in s.daily_lands])}")
    return 0


def cmd_undo(_: argparse.Namespace) -> int:
    """Reverse the last safely-reversible console action (assign / stop / go / an
    unplanned inbox line) -- refuses anything a lane may already have acted on."""
    config.ensure_dirs()
    from zecor.core import actionlog

    r = actionlog.undo_last()
    print(r.message)
    return 0 if r.ok else 1


def cmd_actions(ns: argparse.Namespace) -> int:
    """Recent mutating actions -- the audit trail behind :undo and the history pane."""
    config.ensure_dirs()
    from zecor.core import actionlog

    for a in actionlog.recent(limit=ns.limit):
        flag = "undone" if a.undone else ("rev" if a.reversible else "  -  ")
        print(f"  {a.ts[:19]}  {flag:6}  {a.verb:12}  {a.args}")
    return 0


def cmd_notify(ns: argparse.Namespace) -> int:
    """Send a test notification and report which push channels accepted it."""
    config.ensure_dirs()
    from zecor import notify

    chans = notify.configured_channels()
    if ns.test:
        r = notify.selftest()
        print(f"transcript turn #{r.transcript_id}")
        print("delivered: " + (", ".join(r.channels) or "(no push channel configured)"))
        for e in r.errors:
            print(f"  ! {e}")
        return 0 if not r.errors else 1
    print("configured push channels: " + (", ".join(chans) or "none"))
    print("  set ZECOR_NTFY_TOPIC=<topic> (ntfy.sh) or ZECOR_NTFY_URL, "
          "or ZECOR_PUSHOVER_TOKEN + ZECOR_PUSHOVER_USER")
    return 0


def cmd_replenish(ns: argparse.Namespace) -> int:
    """Provision the next roadmap Move with cards -- the loop's self-feeding step.
    `--status` lists stuck Moves; `--unstick <slug>` clears one."""
    config.ensure_dirs()
    from zecor.core import replenish

    if ns.unstick:
        n = replenish.clear_stuck(ns.unstick)
        print(f"cleared {n} stuck marker(s) for {ns.unstick}")
        return 0
    stuck = replenish.stuck_moves()
    if ns.status:
        print("stuck Moves: " + (", ".join(sorted(stuck)) or "none"))
        return 0
    if ns.dry_run:
        from zecor.core import roadmap, view

        mv = replenish.next_move(roadmap.load(), view.cards(), stuck=stuck)
        print(f"next Move to provision: {mv.slug if mv else '(nothing needs work)'}")
        return 0
    try:
        r = replenish.replenish_once(low_water=ns.low_water, batch=ns.batch)
    except replenish.ReplenishError as e:
        print(f"transient: {e} -- retry next tick")
        return 1
    if r.acted:
        print(f"planned {len(r.created)} card(s) under {r.move}: {', '.join(r.created)}")
    else:
        print(f"no-op: {r.reason}" + ("  (Move marked stuck)" if r.stuck else ""))
    return 0


def cmd_ci(ns: argparse.Namespace) -> int:
    """Run a repo's `.zecor/ci.sh` (or `.zecor/verify.sh`) -- the 'CI green' primitive
    the future auto-merge gate will call."""
    from zecor import ci

    try:
        repo_dir = resolve(ns.repo)
    except RepoNotFound as e:
        print(e)
        return 1
    r = ci.run(repo_dir)
    print(f"{r.script or '(no script)'}: {'green' if r.ok else r.reason}")
    return 0 if r.ok else 1


def cmd_fanout(ns: argparse.Namespace) -> int:
    """One requirement -> a card in each client repo, sharing a fan-out id for the
    cross-repo parity check -- `zecor run` builds them, then `zecor fanout-status <id>`."""
    from zecor.core import intents

    config.ensure_dirs()
    res = intents.fanout(ns.requirement, ns.repos, move=ns.move or None)
    print(res.message)
    return 0 if res.ok else 1


def cmd_fanout_status(ns: argparse.Namespace) -> int:
    from zecor.core import intents

    config.ensure_dirs()
    res = intents.fanout_status(ns.id)
    print(res.message)
    return 0 if res.ok else 1


def cmd_race(ns: argparse.Namespace) -> int:
    """Run two approaches to one card in parallel lanes, keep the first that goes green.
    Experimental (ZECOR_RACING=1) -- the winner is a committed lane branch to review."""
    from zecor import racing
    from zecor.resources import advise_lanes

    config.ensure_dirs()
    if not racing.can_race(advise_lanes()):
        print("racing off or the governor won't allow 2 lanes -- set ZECOR_RACING=1")
        return 1
    path = config.TASKS_DIR / f"{ns.id}.md"
    if not path.exists():
        print(f"no card {ns.id}")
        return 1
    card = Card.parse(path.read_text())
    try:
        repo_dir = resolve(card.repo)
    except RepoNotFound as e:
        print(e)
        return 1
    res = racing.race(card, repo_dir)
    for n in res.notes:
        print(f"  {n}")
    if res.won:
        print(f"winner: {res.winner_label} on branch {res.winner_branch} -- review it")
        return 0
    print("no approach won")
    return 1


def cmd_rules(ns: argparse.Namespace) -> int:
    """T0 rules: list candidate + active, scaffold an active rule from a candidate, or
    show which active rules a repo/intent would match."""
    from zecor import t0

    config.ensure_dirs()
    if ns.sub == "list":
        cand = sorted(config.RULES_DIR.glob("candidate-*.md")) if config.RULES_DIR.exists() else []
        active = t0.load_active()
        print(f"active ({len(active)}):")
        for r in active:
            pin = f"  intent~{r.intent_re}" if r.intent_re else ""
            print(f"  {r.rule_id:28} repos={','.join(r.repos)}{pin}  [{r.path.name}]")
        print(f"candidates awaiting confirmation ({len(cand)}):")
        for c in cand:
            print(f"  {c.stem.removeprefix('candidate-')}  [{c.name}]")
        return 0
    if ns.sub == "check":
        matches = t0.select(t0.load_active(), ns.repo, ns.intent or "")
        if not matches:
            print(f"no active rule matches {ns.repo!r} / {ns.intent!r}")
            return 0
        for r in matches:
            print(f"{r.rule_id}  [{r.path.name}]")
        return 0
    # promote: scaffold rules/active/<name>.yml from a candidate's example diffs
    src = config.RULES_DIR / f"candidate-{ns.sig}.md"
    if not src.exists():
        print(f"no candidate {ns.sig} under {config.RULES_DIR}")
        return 1
    dst = t0.active_dir()
    dst.mkdir(parents=True, exist_ok=True)
    out = dst / f"{ns.name}.yml"
    if out.exists():
        print(f"{out} already exists")
        return 1
    out.write_text(
        f"id: {ns.name}\nlanguage: python\n"
        "# zecor: repos = *\n"
        "# zecor: intent = ~REPLACE-with-a-regex-or-delete-this-line\n"
        "rule:\n  pattern: \"REPLACE with the structural pattern\"\n"
        "fix: \"REPLACE with the rewrite\"\n"
        f"# scaffolded from {src.name} -- tune the pattern/fix, test with "
        f"`zecor rules check <repo> <intent>`, then it goes live.\n"
    )
    print(f"scaffolded {out} from {src.name} -- edit the pattern/fix, then it is active")
    return 0


def cmd_routestats(_: argparse.Namespace) -> int:
    """Show the learned per-tier land rates that ZECOR_EV_ROUTING would use."""
    from zecor import routestats
    from zecor.ledger import Ledger

    config.ensure_dirs()
    if not config.DB_PATH.exists():
        print("no ledger yet")
        return 0
    led = Ledger(config.DB_PATH)
    try:
        raw = led.route_stats()
    finally:
        led.close()
    if not raw:
        print("no attempts recorded yet")
        return 0
    stats = {k: routestats.TierStat(*v) for k, v in raw.items()}
    print(f"EV routing: {'ON' if routestats.enabled() else 'off (set ZECOR_EV_ROUTING=1)'}")
    print(f"{'tier/model':28} {'trials':>7} {'wins':>5} {'rate':>6} {'cost':>5} {'EV':>6}")
    for key in sorted(stats, key=lambda k: -routestats.ev(k, stats)):
        s = stats[key]
        label = f"{key[0]}/{key[1] or '-'}"
        cost = routestats.COST.get(key, routestats.COST.get((key[0], None), 1.0))
        print(
            f"{label:28} {s.trials:>7} {s.wins:>5} {s.land_rate():>6.2f} "
            f"{cost:>5.1f} {routestats.ev(key, stats):>6.2f}"
        )
    return 0


def cmd_retrieval(ns: argparse.Namespace) -> int:
    """Show what retrieval would put in a card's allow_files for a query."""
    from zecor.retrieval import lexical_files, suggest

    try:
        repo_dir = resolve(ns.repo)
    except RepoNotFound as e:
        print(e)
        return 1
    lex = lexical_files(repo_dir, ns.query)
    full = suggest(repo_dir, ns.query)
    print(f"lexical (symbol hits): {', '.join(lex) or '(none)'}")
    print(f"suggested allow_files:  {', '.join(full) or '(none)'}")
    return 0


def cmd_provenance(ns: argparse.Namespace) -> int:
    """AI-code provenance: dump the release export, or show one commit's note."""
    import subprocess

    from zecor import provenance

    config.ensure_dirs()
    if ns.sub == "show":
        try:
            repo_dir = resolve(ns.repo)
        except RepoNotFound as e:
            print(e)
            return 1
        r = subprocess.run(
            ["git", "notes", f"--ref={provenance.NOTES_REF}", "show", ns.commit],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        print(r.stdout.strip() or f"(no provenance note on {ns.commit})")
        return 0 if r.returncode == 0 else 1
    # export: cat every provenance/*.jsonl (optionally one month)
    d = config.ZECOR_ROOT / "provenance"
    files = sorted(d.glob(f"{ns.month}.jsonl" if ns.month else "*.jsonl")) if d.exists() else []
    if not files:
        print("no provenance records yet")
        return 0
    for f in files:
        sys.stdout.write(f.read_text())
    return 0


def cmd_console(ns: argparse.Namespace) -> int:
    """The Command Center TUI -- needs the `console` extra (textual)."""
    config.ensure_dirs()
    try:
        import textual  # noqa: F401  -- the only optional dep the console needs
    except ModuleNotFoundError:
        print("zecor console needs the console extra:  uv sync --group console")
        return 1
    from zecor.console.app import run as run_console

    return run_console(read_only=ns.read_only, ambient=ns.ambient)


def cmd_preflight(_: argparse.Namespace) -> int:
    blockers, warns = _preflight()
    for w in warns:
        print(f"warn:  {w}")
    for b in blockers:
        print(f"BLOCK: {b}")
    if blockers:
        print("not ready -- `zecor run` would abort (use --force to override)")
        return 1
    print("preflight ok")
    return 0


def cmd_resources(ns: argparse.Namespace) -> int:
    """Live RAM / load / swap / LM Studio footprint, and a go/no-go for a `kind` of
    work -- exit 0 = can start, 1 = wait. `--json` for scripts."""
    import json as _json

    s = snapshot()
    if ns.can:
        ok, why = can_start(ns.can, snap=s)
        if ns.json:
            print(_json.dumps({"kind": ns.can, "ok": ok, "why": why, **s.as_dict()}))
        else:
            print(f"{'YES' if ok else 'NO '}  {ns.can}: {why}")
        return 0 if ok else 1
    if ns.json:
        print(_json.dumps({"advise_lanes": advise_lanes(snap=s), **s.as_dict()}))
        return 0
    print(
        f"RAM   {s.ram_free_gb:.1f}GB free / {s.ram_total_gb:.0f}GB   "
        f"swap {s.swap_used_gb:.2f}GB"
    )
    print(f"load  {s.load1:.2f} on {s.cpu_count} cores  (ratio {s.load_ratio:.2f})")
    print(f"disk  {s.disk_free_gb:.0f}GB free")
    print(f"LM    {s.lmstudio_vram_gb:.1f}GB: {', '.join(s.lmstudio_models) or '(none)'}")
    print(f"local-model jobs running: {s.local_consumers}")
    if s.probe_errors:
        print(f"probe errors: {s.probe_errors}")
    print(f"advise_lanes -> {advise_lanes(snap=s)}")
    for k in ("local_lane", "frontier_review"):
        ok, why = can_start(k, snap=s)
        print(f"  can_start({k}): {'yes' if ok else 'no'} -- {why}")
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    """Which tiers can Zecor reach right now? No tokens spent. OAuth tiers (T3 Codex, T4
    Claude, T2b agy) can lapse; this prints the exact re-auth command for any that are
    down."""
    import json as _json
    import os
    import subprocess
    import urllib.request

    from zecor.localmodel import available

    fixes: list[str] = []

    def line(tier: str, ok: bool, detail: str, fix: str = "") -> None:
        print(f"  {'ok  ' if ok else 'MISS'} {tier:12s} {detail}")
        if not ok and fix:
            fixes.append(fix)

    line(
        "T1 local",
        available(),
        "LM Studio :1234",
        "open LM Studio, then: lms server start --port 1234",
    )

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    t2a_ok, detail = False, "no GEMINI_API_KEY / GOOGLE_API_KEY"
    if key:
        try:
            req = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": key},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                t2a_ok = bool(_json.loads(r.read()).get("models"))
            detail = "AI Studio key valid" if t2a_ok else "key set but no models returned"
        except Exception as e:
            detail = f"key set but request failed: {str(e)[:80]}"
    line(
        "T2a gemini",
        t2a_ok,
        detail,
        "put GEMINI_API_KEY=<key> in ~/.config/secrets/personal.env "
        "(new key: aistudio.google.com/apikey)",
    )

    for tier, cmd, needle, fix in (
        ("T3 codex", ["codex", "login", "status"], "Logged in", "codex login"),
        ("T4 claude", ["claude", "auth", "status"], '"loggedIn": true', "claude auth login"),
        ("T2b agy", ["agy", "models"], "gemini", "agy   (then complete the browser sign-in)"),
    ):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=25, check=False)
            blob = (p.stdout + p.stderr).lower()
            ok = p.returncode == 0 and needle.lower() in blob
            line(tier, ok, "signed in" if ok else "NOT signed in / needs re-auth", fix)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            line(tier, False, "not installed / timed out", fix)

    if fixes:
        print("\nto fix:")
        for f in fixes:
            print(f"  {f}")
    else:
        print("\nall tiers authed.")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    from zecor.core import view

    config.ensure_dirs()
    h = view.health()
    print(f"STOP flag: {'SET -- zecor will halt' if h.stopped else 'clear'}")
    print(f"disk free: {h.disk_free_gb} GB (floor {h.disk_floor_gb})")
    print("task cards: " + (", ".join(f"{k}={v}" for k, v in sorted(h.counts.items())) or "none"))
    q = view.quota()
    print("learned quota:" if q else "learned quota: nothing yet")
    for row in q:
        print(
            f"  {row.vendor:16s} {row.window:6s} "
            f"used={row.used} cap={row.cap} resets={row.resets_at}"
        )
    return 0


DEFER_COOLDOWN_H = 1  # a deferred card waits at least this long before it is retried
DEFER_PROMOTE_AFTER = 5  # this many consecutive defers -> needs-human (stop the churn)


def _ready_cards() -> list[Card]:
    # `deferred` cards are retried on the next run -- a dry window must not lose work --
    # but not before their retry_after, or a launchd loop re-burns the escalation ladder
    # every few minutes on a card that is only going to defer again.
    now = datetime.now(UTC)
    all_cards = [Card.parse(p.read_text()) for p in sorted(config.TASKS_DIR.glob("[0-9]*.md"))]
    state_by_id = {c.id: c.state for c in all_cards}
    out: list[Card] = []
    for c in all_cards:
        if not (c.state == "ready" or (c.state == "deferred" and _retry_due(c, now))):
            continue
        # hold a card until every card it depends on has landed -- same-file cards run
        # serially so they don't produce conflicting branches (planner._link_file_overlaps).
        if any(state_by_id.get(dep) != "landed" for dep in c.depends_on):
            continue
        out.append(c)
    return out


def _retry_due(card: Card, now: datetime) -> bool:
    if not card.retry_after:
        return True
    try:
        due = datetime.fromisoformat(card.retry_after)
    except ValueError:
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return now >= due


def _apply_outcome(card: Card, state: str) -> None:
    """A card that keeps deferring is promoted to needs-human after DEFER_PROMOTE_AFTER
    so it surfaces in the digest, not churn."""
    if state == "deferred":
        card.defer_count += 1
        if card.defer_count >= DEFER_PROMOTE_AFTER:
            card.state = "needs-human-design"
            card.retry_after = None
        else:
            card.state = "deferred"
            card.retry_after = (
                datetime.now(UTC) + timedelta(hours=DEFER_COOLDOWN_H)
            ).isoformat()
    else:
        card.state = state
        card.retry_after = None
        if state == "landed":
            card.defer_count = 0


def _mark_card(card: Card, state: str) -> Card:
    """Persist the run outcome through the serialized writer so a `zecor retry` from the
    console cannot race the orchestrator into a lost update. Returns the persisted card
    (re-read from disk under the lock) -- the caller should use it, not its own copy."""
    from zecor.core import cards as core_cards

    try:
        return core_cards.mutate(
            card.id, lambda c: _apply_outcome(c, state), base_rev=card.rev
        )
    except core_cards.StaleRev:
        # the card moved under us (concurrent `zecor retry` or a second run) -- its
        # newer intent wins, so the outcome still lands in the ledger/digest but we
        # don't stamp a stale state back onto the card.
        try:
            return core_cards.read(card.id)
        except core_cards.CardNotFound:
            return card
    except core_cards.CardNotFound:
        # the .md vanished mid-run -- do not recreate it; the outcome is already in the
        # ledger and digest.
        return card


def _parse_until(s: str | None) -> datetime | None:
    """HH:MM in LOCAL time -- launchd's --until 07:00 means 07:00 where the laptop is.
    `combine(...).astimezone()` re-resolves the UTC offset for the target date, so a DST
    change in the overnight window doesn't shift 07:00 by an hour (which
    `now.replace(hour=...)` would)."""
    if not s:
        return None
    parts = s.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        print(f"--until {s!r}: want HH:MM -- ignoring, no time bound this run")
        return None
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        print(f"--until {s!r}: HH must be 0-23, MM 0-59 -- ignoring, no time bound this run")
        return None
    now = datetime.now().astimezone()
    target = datetime.combine(now.date(), time(hh, mm)).astimezone()
    if target <= now:
        target = datetime.combine(now.date() + timedelta(days=1), time(hh, mm)).astimezone()
    return target


def _disk_free_gb() -> int:
    return shutil.disk_usage(Path.home()).free // (1024**3)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zecor", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decompose", help="turn a requirements doc into proposed inbox lines")
    d.add_argument("doc", help="path to a markdown requirements doc (or a name under projects/)")
    d.set_defaults(fn=cmd_decompose)
    ini = sub.add_parser("init", help="one command from a fresh clone to a runnable first task")
    ini.add_argument("--repo", help="name of an existing checkout under ~/code to queue a task for")
    ini.add_argument("--yes", action="store_true", help="also run the fixable `zecor doctor` gaps")
    ini.set_defaults(fn=cmd_init)
    sub.add_parser("plan", help="expand queue/inbox.md into task cards").set_defaults(fn=cmd_plan)
    r = sub.add_parser("run", help="work the ready queue")
    r.add_argument("--until", metavar="HH:MM", help="stop at this wall-clock time")
    r.add_argument("--max", type=int, default=0, help="stop after N tasks (0 = no limit)")
    r.add_argument("--force", action="store_true", help="run even if preflight finds a blocker")
    r.set_defaults(fn=cmd_run)
    pf = sub.add_parser("preflight", help="are the local model / disk / swap ok to start?")
    pf.set_defaults(fn=cmd_preflight)
    sub.add_parser("digest", help="re-render today's digest").set_defaults(fn=cmd_digest)
    rt = sub.add_parser("retry", help="re-queue a needs-human/blocked card, force a tier")
    rt.add_argument("id", help="card id, e.g. 0042")
    rt.add_argument("--tier", default=None, help="force the first attempt: t1|t3|t4|t2b")
    rt.add_argument("--model", default=None, help="force the model within that tier (e.g. opus)")
    rt.add_argument("--note", default=None, help="a constraint to carry into the loop")
    rt.set_defaults(fn=cmd_retry)
    sub.add_parser("status", help="quota state, queue depth, STOP flag").set_defaults(fn=cmd_status)
    sub.add_parser("stop", help="set the STOP flag -- zecor halts between tasks").set_defaults(
        fn=cmd_stop
    )
    sub.add_parser("go", help="clear the STOP flag").set_defaults(fn=cmd_go)
    cons = sub.add_parser("console", help="the Command Center TUI (needs the console extra)")
    cons.add_argument("--read-only", action="store_true", help="monitor mode -- no changes")
    cons.add_argument("--ambient", action="store_true", help="glanceable, low-chrome")
    cons.set_defaults(fn=cmd_console)
    sub.add_parser("routestats", help="learned per-tier land rates (EV routing)").set_defaults(
        fn=cmd_routestats
    )
    sc = sub.add_parser("scaffold", help="lay down a from-scratch project with a green gate")
    sc.add_argument("name")
    sc.add_argument("description")
    sc.add_argument(
        "--kind",
        choices=["python-cli", "python-lib", "api-service", "web-spa", "android-app", "ios-app"],
        default="python-cli",
    )
    sc.add_argument("--dest", default="", help="parent dir (default: cwd)")
    sc.set_defaults(fn=cmd_scaffold)
    ci_p = sub.add_parser("ci", help="run a repo's .zecor/ci.sh (the 'CI green' check)")
    ci_p.add_argument("repo")
    ci_p.set_defaults(fn=cmd_ci)
    sub.add_parser("metrics", help="land rate / cycle time / cost from the ledger").set_defaults(
        fn=cmd_metrics
    )
    sub.add_parser("budget", help="the unattended spend ceiling state").set_defaults(
        fn=cmd_budget
    )
    sub.add_parser("progress", help="daily roadmap progress pass -- log + flag stuck").set_defaults(
        fn=cmd_progress
    )
    gt = sub.add_parser("git", help="in-process git queries (gitoxide, with a git fallback)")
    gts = gt.add_subparsers(dest="sub", required=True)
    _bs = gts.add_parser("branch-state", help="ahead/behind/merged vs. a base")
    _bs.add_argument("branch")
    _bs.add_argument("--base", default="main")
    _cf = gts.add_parser("changed-files", help="files that differ between two revs")
    _cf.add_argument("a")
    _cf.add_argument("b")
    _rp = gts.add_parser("rev-parse", help="resolve a rev to a sha")
    _rp.add_argument("rev", nargs="?", default="HEAD")
    _lg = gts.add_parser("log", help="recent commits, newest first")
    _lg.add_argument("rev", nargs="?", default="HEAD")
    _lg.add_argument("--limit", type=int, default=20)
    gts.add_parser("status", help="staged / unstaged / untracked working-tree state")
    gt.add_argument("--repo", default=".", help="repo path (default: cwd)")
    gt.set_defaults(fn=cmd_git)
    sub.add_parser("mode", help="show the active security posture").set_defaults(fn=cmd_mode)
    scn = sub.add_parser("scan", help="scan a file / stdin / diff / tree for secrets")
    scn.add_argument("path", nargs="?", help="file to scan (default: stdin)")
    scn.add_argument("--diff", action="store_true", help="scan `git diff` output instead")
    scn.add_argument("--cached", action="store_true", help="with --diff, scan the staged diff")
    scn.add_argument("--dir", help="walk a directory tree (gitignore-aware, parallel)")
    scn.add_argument("--injection", action="store_true", help="also flag prompt-injection phrasing")
    scn.add_argument("--no-gitignore", action="store_true", help="with --dir, ignore .gitignore")
    scn.add_argument("--sarif", action="store_true", help="emit SARIF 2.1.0 instead of text")
    scn.set_defaults(fn=cmd_scan)
    sb = sub.add_parser("sandbox", help="run a command under OS-level isolation (zecor-sandbox)")
    sbs = sb.add_subparsers(dest="sub", required=True)
    sbsh = sbs.add_parser("show", help="print the policy + backend the sandbox would enforce")
    sbr = sbs.add_parser("run", help="run `-- CMD [ARGS...]` confined to the workdir")
    sbr.add_argument("cmd", nargs=argparse.REMAINDER, help="-- then the command to run")
    sbr.add_argument("--dry-run", action="store_true", help="print the plan as JSON, run nothing")
    sbr.add_argument("--audit", help="write a JSON record of the policy + outcome to this file")
    for _p in (sbsh, sbr):
        _p.add_argument("--workdir", default=".", help="read+write root for the child")
        _p.add_argument("--allow-net", action="store_true", help="permit outbound network")
    sb.set_defaults(fn=cmd_sandbox)
    ct = sub.add_parser("cost", help="price a file / stdin against a model's list price")
    ct.add_argument("path", nargs="?", help="file to price (default: stdin)")
    ct.add_argument("--model", default="claude-sonnet-4", help="pricing model key")
    ct.add_argument("--out-tokens", type=int, default=0, help="expected completion length")
    ct.add_argument("--enc", default="cl100k_base", help="encoding or tokenizer.json path")
    ct.set_defaults(fn=cmd_cost)
    sub.add_parser("doctor", help="check every dependency + the fix for each gap").set_defaults(
        fn=cmd_doctor
    )
    ins = sub.add_parser("install", help="act on what `zecor doctor` flags (dry-run unless --yes)")
    ins.add_argument("--yes", action="store_true", help="execute the non-interactive steps")
    ins.set_defaults(fn=cmd_install)
    rm = sub.add_parser("remote", help="set up the phone path (Tailscale + mosh + tmux)")
    rms = rm.add_subparsers(dest="sub")
    setup = rms.add_parser("setup", help="check / guide the setup, print the connection recipe")
    setup.add_argument("--install", action="store_true", help="run the brew installs")
    setup.add_argument("-y", "--yes", action="store_true", help="install without prompting")
    rms.add_parser("string", help="just print the mosh+tmux connection string")
    rm.set_defaults(fn=cmd_remote, sub="setup", install=False, yes=False)
    sv = sub.add_parser("supervise", help="keep-alive loop: run + replenish + watch + progress")
    sv.add_argument("--once", action="store_true", help="one iteration then exit")
    sv.add_argument("--status", action="store_true", help="print the heartbeat and exit")
    sv.add_argument("--run-hours", type=float, default=6.0, dest="run_hours")
    sv.set_defaults(fn=cmd_supervise)
    wd = sub.add_parser("watchd", help="always-on watchers -> transcript (no console needed)")
    wd.add_argument("--once", action="store_true")
    wd.add_argument("--interval", type=float, default=30.0)
    wd.set_defaults(fn=cmd_watchd)
    sub.add_parser("undo", help="reverse the last safely-reversible action").set_defaults(
        fn=cmd_undo
    )
    ac = sub.add_parser("actions", help="recent mutating actions (audit trail)")
    ac.add_argument("--limit", type=int, default=30)
    ac.set_defaults(fn=cmd_actions)
    nt = sub.add_parser("notify", help="push-notification channels -- --test sends one")
    nt.add_argument("--test", action="store_true", help="send a test notification now")
    nt.set_defaults(fn=cmd_notify)
    rp = sub.add_parser("replenish", help="provision the next roadmap Move with cards")
    rp.add_argument("--low-water", type=int, default=2, help="provision below this many live cards")
    rp.add_argument("--batch", type=int, default=3, help="cards to add per tick")
    rp.add_argument("--dry-run", action="store_true", help="name the next Move, plant nothing")
    rp.add_argument("--status", action="store_true", help="list stuck Moves")
    rp.add_argument("--unstick", metavar="SLUG", default="", help="clear a stuck Move")
    rp.set_defaults(fn=cmd_replenish)
    dp = sub.add_parser("deploy", help="build a repo at a sha and roll it to Cloud Run")
    dp.add_argument("repo")
    dp.add_argument("sha")
    dp.set_defaults(fn=cmd_deploy)
    dps = sub.add_parser("deploys", help="held deploys -- list, or --approve / --reject an id")
    dps.add_argument("--approve", type=int, default=None, metavar="ID")
    dps.add_argument("--reject", type=int, default=None, metavar="ID")
    dps.set_defaults(fn=cmd_deploys)
    fo = sub.add_parser("fanout", help="one requirement -> a card in each client repo")
    fo.add_argument("requirement")
    fo.add_argument("repos", nargs="+", help="two or more repo names")
    fo.add_argument("--move", default="", help="roadmap Move slug to file all cards under")
    fo.set_defaults(fn=cmd_fanout)
    fos = sub.add_parser("fanout-status", help="state + parity of a fan-out group")
    fos.add_argument("id")
    fos.set_defaults(fn=cmd_fanout_status)
    rc = sub.add_parser("race", help="run 2 approaches to a card in parallel (ZECOR_RACING=1)")
    rc.add_argument("id")
    rc.set_defaults(fn=cmd_race)
    ru = sub.add_parser("rules", help="T0 deterministic rules: list / promote / check")
    rus = ru.add_subparsers(dest="sub", required=True)
    rus.add_parser("list", help="active rules + candidates awaiting confirmation")
    rup = rus.add_parser("promote", help="scaffold rules/active/<name>.yml from a candidate")
    rup.add_argument("sig", help="candidate signature (16 hex)")
    rup.add_argument("name", help="rule id / filename stem")
    ruc = rus.add_parser("check", help="which active rules match a repo/intent")
    ruc.add_argument("repo")
    ruc.add_argument("intent", nargs="?", default="")
    ru.set_defaults(fn=cmd_rules)
    rv = sub.add_parser("retrieval", help="preview allow_files retrieval for a query")
    rv.add_argument("repo")
    rv.add_argument("query")
    rv.set_defaults(fn=cmd_retrieval)
    pv = sub.add_parser("provenance", help="AI-code provenance export / lookup")
    pvs = pv.add_subparsers(dest="sub", required=True)
    pve = pvs.add_parser("export", help="print provenance/*.jsonl (the release record)")
    pve.add_argument("--month", default="", help="YYYY-MM (default: all)")
    pvsh = pvs.add_parser("show", help="show one commit's provenance note")
    pvsh.add_argument("repo")
    pvsh.add_argument("commit")
    pv.set_defaults(fn=cmd_provenance)
    sub.add_parser("check", help="which tiers are reachable (no tokens spent)").set_defaults(
        fn=cmd_check
    )
    a = sub.add_parser("ask", help="one-shot prompt at one tier -- poke a model directly")
    a.add_argument("prompt", nargs="?", help="the prompt (or pipe it on stdin)")
    a.add_argument("--tier", default="local", help="local | gemini | claude  (default local)")
    a.add_argument("--model", default=None, help="override the tier's default model")
    a.add_argument("--max-tokens", type=int, default=800)
    a.add_argument("--timeout", type=int, default=120)
    a.set_defaults(fn=cmd_ask)
    sv = sub.add_parser("serve", help="the command-center HTTP API + PWA (behind Tailscale)")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument(
        "--bind", default="127.0.0.1", help="0.0.0.0 or a Tailscale IP for remote access"
    )
    sv.add_argument("--token", default=None, help="require ?t=<token> on every request")
    sv.set_defaults(fn=cmd_serve)
    iv = sub.add_parser("inventory", help="architecture legwork: cycles, imports, churn")
    iv.add_argument("repo", help="target repo name")
    iv.add_argument("--topic", default=None, help="label for digest/architecture-<topic>.md")
    iv.set_defaults(fn=cmd_inventory)
    rs = sub.add_parser("resources", help="live RAM/load/swap + go/no-go for fan-out")
    rs.add_argument("--can", metavar="KIND", default=None,
                    help="check headroom for one kind (local_lane, frontier_review, ...)")
    rs.add_argument("--json", action="store_true", help="machine-readable")
    rs.set_defaults(fn=cmd_resources)
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    fn = ns.fn
    return int(fn(ns))


if __name__ == "__main__":
    sys.exit(main())