# Security

Zecor runs models against source code and, in some modes, against attacker-controlled
text. This document states the threat model, the isolation boundaries, and how to
report a vulnerability.

## Reporting a vulnerability

Email `security@zecor.dev` with a description and a reproduction. Do not open a public
issue for an unpatched vulnerability. Expect an acknowledgement within 72 hours and a
fix or mitigation timeline within 7 days. Coordinated disclosure is the default; credit
is given unless you decline it.

## Threat model

Zecor's inputs, ranked by trust:

1. **The maintainer's own machine and config** — trusted. `zecor.config.json`, env
   vars, the local model endpoint, vendor CLI credentials.
2. **The target repository's source** — semi-trusted. Zecor reads it, and in `guarded`
   and `greenfield` modes writes to a branch of it. A malicious dependency or a
   poisoned test can run code during the deterministic gate; that is the repository's
   own supply-chain surface, not Zecor's to fix, but Zecor runs the gate in a capped
   subprocess with a stripped environment.
3. **External text** — hostile. GitHub issues, PR comments, bug reports, fetched web
   pages. Treated as data, never as instructions (see *Prompt injection* below).

Assets to protect: the maintainer's vendor credentials and local secrets; the integrity
of `main` in the target repo; the maintainer's spend.

## Autonomy modes

Set with `ZECOR_MODE` or the `mode` key in `zecor.config.json`. Default: `hardened`.

| Mode | Use case | Writes | Auto-merge | Shell | External input |
|---|---|---|---|---|---|
| `greenfield` | New app, zero users, isolated workspace | files, branches, PRs | yes | unrestricted | trusted |
| `guarded` | Feature work on an existing repo | branch + author + test | only if every deterministic check passes clean | unrestricted | wrapped as untrusted |
| `hardened` | Production, public repos, untrusted input | branch, draft PR only | never (human review required) | allow-listed | wrapped as untrusted |

`zecor mode` prints the active posture.

## Isolation

- **Subprocess capping.** Every external command runs via `workers.base.run_capped`:
  its own process group, a hard wall-clock timeout, `SIGKILL` on overrun, and an
  environment with `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` stripped so a lane cannot
  reach the maintainer's paid vendor accounts.
- **Command allow-list (`hardened`).** `zecor.toolgate` refuses any binary or leading
  argument not on an explicit whitelist (git read/write subcommands, the deterministic
  gates, the known tier appliers). A refusal returns exit 126 before anything runs.
- **MCP disabled.** Model CLIs are invoked with MCP config emptied — tool definitions
  are not injected into any prompt.
- **Workspace expectation.** `greenfield` and `hardened` expect to run in a container
  or a throwaway checkout. `guarded` operates on a real checkout but only on its own
  branch and never merges autonomously.

## Prompt injection and untrusted input

`zecor.untrusted.wrap(source, text)` fences external text in a token-delimited container
with a per-call nonce and a standing instruction to treat everything inside strictly as
data. Marker-shaped lines and role-boundary spoofs (`<|im_start|>`, fenced `system`
blocks) inside the payload are defanged. Wrapping is applied at every ingestion point
whenever `modes.treat_external_input_as_hostile()` is true — that is, in every mode
except `greenfield`.

## Secret-leak prevention

`zecor.secretscan` runs before any draft PR is opened. It scans the committed diff and
the generated PR body for known credential shapes (AWS, GitHub, Slack, Google, OpenAI,
Anthropic keys; private-key headers; `secret = "…"` assignments) and for high-entropy
tokens. A hit flips the task to `needs-human`: the verified change stays on its branch,
but no PR carries the leak. `zecor scan` runs the same check by hand on a file, stdin,
or `git diff` output.

## Known limitations

- **Secret scan.** Pattern- and entropy-based; it catches the common credential shapes
  and a random one-line blob, but a secret split across lines into low-entropy chunks
  can evade it. It is a backstop, not a guarantee. Run a full scanner (Gitleaks,
  detect-secrets) in CI as well.
- **Command allow-list.** In `hardened` mode `run_capped` pins a trusted `PATH`
  (system dirs plus the project venv), but it does not defend against an attacker who
  can already place an executable in a system directory or the venv.
- **`greenfield` mode is unsandboxed by Zecor.** It expects to run inside a container
  or throwaway VM that you provide.

## What Zecor does not defend against

- A compromised local model endpoint or a malicious vendor CLI on the maintainer's
  `PATH`.
- Supply-chain attacks in the target repository's own dependencies executed by its own
  test suite.
- A maintainer who runs `greenfield` mode against a repository with real users.
