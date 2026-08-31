# Zecor

Zecor is an autonomous software construction engine that pairs local deterministic
execution with hybrid model routing to build and verify software with minimal token
overhead.

Apache-2.0. Project home: [zecor.dev](https://zecor.dev).

## How it works

```
  one-line ask
       │
       ▼
  ┌──────────────────┐   fails fast, no tokens spent
  │ Deterministic    │──────────────────────────────┐
  │ gate             │   ast-grep codemods,         │
  │ (.zecor/verify.sh)│  linters, type checks, tests │
  └──────────────────┘                              │
       │ needs a model                              │
       ▼                                            │
  ┌──────────────────┐   local, no metering         │
  │ Local model tier │   aider + a coder model on   │
  │ (LM Studio /      │   an OpenAI-compatible        │
  │  Ollama / vLLM)   │   endpoint                    │
  └──────────────────┘                              │
       │ two local attempts, then escalate          │
       ▼                                            │
  ┌──────────────────┐   metered, last resort       │
  │ Cloud reasoning  │   Codex / Claude, one paid   │
  │ fallback         │   call at a time              │
  └──────────────────┘                              │
       │                                            │
       ▼                                            ▼
  branch + draft PR  ◄──── secret scan ──── deterministic gate re-run
```

The deterministic gate is the deliverable: a task is done when the gate goes green.
Most work never reaches a model. Escalation is driven by artifacts the model cannot
fake — a still-red gate, an unchanged failing-assertion set, a duplicate diff, scope
creep past the allow-list — not by self-reported confidence.

## Quickstart

Requires Python 3.12+, `git`, `ripgrep`, `ast-grep`, and an OpenAI-compatible local
model endpoint (LM Studio, Ollama with `/v1`, vLLM, llama.cpp).

```
git clone https://github.com/zecordev/zecor && cd zecor
uv sync --group dev
cp zecor.config.example.json zecor.config.json     # edit to taste
uv run zecor doctor                                 # check the toolchain
```

Run against a repository:

```
uv run zecor scaffold "widget tool" "renders widgets" ~/code   # optional: new repo
echo "- widget-tool: add a --json flag to the CLI" >> queue/inbox.md
uv run zecor plan                                    # inbox -> scoped task cards
uv run zecor run --until 07:00                       # work the queue, bounded
```

`uv run zecor console` opens the operator TUI. `uv run zecor serve --bind <ip> --token
<secret>` serves the JSON API and the PWA for remote control.

## Autonomy and security modes

| Mode | Use case | Writes | Auto-merge | Shell | External input |
|---|---|---|---|---|---|
| `greenfield` | New app, zero users, isolated workspace | files, branches, PRs | yes | unrestricted | trusted |
| `guarded` | Feature work on an existing repo | branch, author, test | only if every deterministic check passes clean | unrestricted | wrapped as untrusted |
| `hardened` | Production, public repos, untrusted input | branch, draft PR only | never (human review required) | allow-listed | wrapped as untrusted |

Default is `hardened`. Set with `ZECOR_MODE` or the `mode` key in `zecor.config.json`.
`zecor mode` prints the active posture. See [SECURITY.md](SECURITY.md) for the threat
model, sandboxing, and prompt-injection handling.

## Local model runtimes

Zecor talks to any OpenAI-compatible `/v1/chat/completions` endpoint. Set
`local_model.base_url` in the config (or `ZECOR_LM_BASE_URL`):

- **LM Studio** — `http://127.0.0.1:1234/v1`, start the server from the app or
  `lms server start`.
- **Ollama** — `http://127.0.0.1:11434/v1`, models pulled with `ollama pull`.
- **vLLM** — `http://127.0.0.1:8000/v1`, launched with `vllm serve <model>`.
- **llama.cpp** — `./llama-server --host 127.0.0.1 --port 8080` then `.../v1`.

Five roles are configured independently (`primary_coder`, `deep_coder`, `reasoner`,
`router`, `embed`); point them at whatever your endpoint serves.

## Configuration

`zecor.config.json` (see `zecor.config.example.json` for the fully documented schema)
covers the local endpoint, deterministic tool paths, gate script, autonomy limits, the
spend ceiling, and notification channels. Environment variables override the file.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [PROSE_STYLE.md](PROSE_STYLE.md). The gate
for this repository is `bash .zecor/verify.sh` (ruff, mypy `--strict`, pytest).
