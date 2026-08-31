# Contributing

## Before you start

Read [PROSE_STYLE.md](PROSE_STYLE.md). It governs every written artifact — commits, PR
text, docstrings, comments, docs — and automated workflows enforce it.

## Development setup

```
uv sync --group dev
git config commit.template .gitmessage
```

The runtime is standard-library only. `uv sync --group dev` adds `ruff`, `mypy`,
`pytest`, and `textual` (needed only by `zecor console`).

## Running the test suite

```
bash .zecor/verify.sh        # ruff check, mypy --strict, pytest -- the full gate
uv run --group dev pytest -q tests/test_modes.py    # a single file
```

The PWA under `webui/` has its own suite: `cd webui && pnpm install && pnpm test`.

## Issues

Include: what you expected, what happened, the exact command, and the output. For a
behaviour change, name the invariant you believe is violated.

## Pull requests

- Branch from `main`. One logical change per PR.
- The description has exactly four sections:
  `### Problem`, `### Solution`, `### Verification & Test Results`,
  `### Breaking Changes / Invariants`. The template enforces this.
- Paste the output of `bash .zecor/verify.sh` under *Verification*.
- Conventional Commits: `type(scope): imperative subject` (<= 50 chars), body wrapped
  at 72 columns explaining *why*.
- A change to `zecor/route.py` is reviewed as a security-sensitive change: it is written
  attended, property-tested against a synthetic ledger, and never changed by an
  automated run.

## Architecture notes

- `zecor/core/` is the read model (`view.py`) and the write verbs (`intents.py`). Every
  surface — CLI, TUI, HTTP API — goes through it. A boundary test fails if a card is
  written from outside `core/`.
- Escalation triggers (`zecor/triggers.py`) are pure functions over externally observed
  artifacts. Do not add a trigger that reads a model's self-assessment.
- Security posture lives in `zecor/modes.py`; the gates there (`automerge_allowed()`,
  `shell_is_allow_listed()`, `treat_external_input_as_hostile()`) are the only
  sanctioned way to branch on mode.
