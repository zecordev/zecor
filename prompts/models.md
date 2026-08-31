# Local model roster — verified on this machine 2026-08-28

Served by LM Studio (MLX) on `http://127.0.0.1:1234/v1`. Slugs are the ids `/v1/models`
returns — use them verbatim. Plan section 1.1 roles.

| Role | Model id | Size | Load |
|---|---|---|---|
| **Primary coder** (bounded class) | `qwen/qwen3-coder-30b` | 17.2 GB | resident |
| **Deep coder** (one slow careful pass) | `qwen/qwen2.5-coder-32b` | 18.5 GB | JIT — never with gpt-oss also loaded |
| **Reasoner / triage** ("why did this fail") | `openai/gpt-oss-20b` | 12.1 GB | JIT |
| **Router / summarizer** (zero-judgment, high volume) | `qwen3-4b-instruct-2507` | 2.3 GB | resident |
| **Embeddings** ("where is the thing that does X") | `text-embedding-nomic-embed-text-v1.5` | 84 MB | resident |
| _optional_ reasoning-4B | `qwen/qwen3-4b` | 2.3 GB | unassigned — cheap first-pass reasoning if wanted |

Resident set ≈ 20 GB (primary + router + embeddings). Wired-memory ceiling is 48 GB
(`iogpu.wired_limit_mb=49152`), so one heavy JIT model fits alongside with room for a
Vite build + headless browser.

## Not available

- **Qwen3-Embedding-0.6B** — only ships as `torchSafetensors`; LM Studio has no runtime
  for it and no MLX build resolves. `nomic-embed-text-v1.5` is the substitute (768-dim,
  works, loaded by default).
- **Qwen3-Reranker-0.6B** — LM Studio doesn't serve rerankers. Rerank with the embedding
  model + cosine, or gpt-oss, until section 7.5 needs better.

## Router note

`qwen3-4b-instruct-2507` is the non-reasoning instruct build — smoke-tested, emits content
with 0 reasoning tokens. Use it, not `qwen/qwen3-4b` (that one is reasoning-capable and
burns the token budget thinking before it answers).

## Serve

    lms server start --port 1234
    curl -s localhost:1234/v1/models | jq -r '.data[].id'
