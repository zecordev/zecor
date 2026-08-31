# Prose style

Every written artifact in this repository — Markdown, docstrings, comments, commit
messages, PR descriptions, CLI help — reads as if a staff systems architect wrote it.
Terse, imperative, precise. Automated workflows, subagents and local models treat this
file as a fixed constraint.

## Voice

Active and imperative. State the constraint or the action, not the intent to describe
it.

- Yes: "Verify the AST before model dispatch."
- No: "In this section we will be verifying the AST before we dispatch to the model."

## Banned

- Conversational filler: "Sure thing", "Here is a breakdown", "Hope this helps", "Let's
  dive in", "Great question".
- Marketing adjectives: "blazing-fast", "seamless", "groundbreaking", "state-of-the-art",
  "game-changing", "robust" as a synonym for "works".
- Trailing summary labels: "In Summary:", "Conclusion:", "TL;DR:" at the end of a file.
- Restating code in a comment: `# set count to zero` above `count = 0`.
- Hedging chains: "it might possibly be the case that perhaps".
- Emoji in source, docs, commits, or PR text.

## Comments

A comment earns its place by explaining something the code cannot:

- an architectural invariant the reader must not break,
- a non-obvious hardware or platform constraint,
- an upstream bug or quirk being worked around,
- why a slower or uglier path was chosen.

Match the comment density of the file you are editing. Do not add explanatory comments
to code that has none.

## Commits

Conventional Commits.

```
type(scope): imperative subject, <= 50 chars

Body wrapped at 72 columns. Explain WHY the change was made and what invariant it
preserves or changes. The diff already shows what changed.
```

`type` is one of `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `perf`, `build`,
`ci`. A breaking change carries a `BREAKING CHANGE:` paragraph in the body.

Install the shared template: `git config commit.template .gitmessage`.

## Pull requests

Exactly four sections, in this order, nothing else:

```
### Problem
### Solution
### Verification & Test Results
### Breaking Changes / Invariants
```

## Line wrapping

- Committed Markdown and commit-message bodies: wrap at 72–100 columns.
- PR descriptions, issue text, review comments on the forge: **one paragraph is one
  line**. A single newline renders as `<br>` under GFM; wrapped text becomes a ragged
  column that ignores the reader's window.
