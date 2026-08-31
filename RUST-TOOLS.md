# SPDX-License-Identifier: Apache-2.0

# Zecor Rust tools — positioning and the road to world-class

Five helpers. Each one already replaces or adds a capability in the engine. This
document is the standard they are held to: what the incumbents are, where they fall
short, and the features that separate a script from a product. Items marked **shipped**
are done; the rest are the committed backlog.

Each tool now lives in its own repo, extracted from this monorepo with
`git subtree split`:

| tool | repo |
|---|---|
| `zecor-astscan`   | https://github.com/zecordev/zecor-astscan |
| `zecor-sandbox`   | https://github.com/zecordev/zecor-sandbox |
| `zecor-tokcount`  | https://github.com/zecordev/zecor-tokcount |
| `zecor-secretscan`| https://github.com/zecordev/zecor-secretscan |
| `zecor-gitengine` | https://github.com/zecordev/zecor-gitengine |

The engine finds the binaries via `zecor/_rustbridge.py`: it looks in
`ZECOR_RUST_BIN_DIR`, then `<repo>/bin/`, then `PATH`. Install them with either the
prebuilt release binaries (Linux / macOS / Windows, attached to each repo's GitHub
release) dropped into `./bin`, or `cargo install --git
https://github.com/zecordev/<tool>` (published crates.io versions once tagged). Every
call site keeps a pure-Python fallback, so the engine runs with or without them.

The through-line: structured JSON I/O, zero network by default, deterministic output,
one small binary per job.

---

## 1. `zecor-astscan` — structural code analysis

**Incumbents.** `tree-sitter` CLI (parse only, no analysis), `ast-grep` (pattern
search/rewrite, not metrics or diff), `semgrep` (rule engine, JVM/Python startup cost,
cloud-tilted), `scc`/`tokei` (line counts, no structure), language-specific tools
(`ruff`, `eslint`) that do not compose across a polyglot repo.

**Gaps people hit.** No single fast tool that, across many languages at once: (a) tells
you a generated file will not parse *before* you run the suite, (b) gives a *structural*
diff (which symbols changed, not which lines), (c) exports a call graph for
change-impact analysis, (d) reports complexity so a reviewer knows where to look. Every
LLM-coding harness reimplements a weak version of (a).

**Shipped.** `check` (Tree-sitter `ERROR`/`MISSING` nodes with line/col, Rayon
fan-out, one report per broken region) and `symbols` (top-level defs across
Python/JS/TS/TSX/Rust/Go with byte spans). **`metrics <file>...`** — per top-level
function: line count, McCabe cyclomatic complexity (branch nodes + short-circuit
`&&`/`||`/`and`/`or`, language-aware node set), max nesting depth, arity (`self`/`cls`
not counted). **`diff <old> <new>`** — structural change set (`added` / `removed` /
`modified`) keyed by `kind:name`, whitespace-normalised so a reindent is not a change.
`zecor.selfreview` now raises an advisory `complexity` finding when a touched function
lands past the caps (complexity 20 / 120 lines / depth 5, all env-overridable).

**Still to world-class.**
- **`callgraph <files>`** — intra-repo caller→callee edges (resolved by name +
  same-file scope; cross-file by import), emitted as JSON adjacency + optional DOT.
- **Signature-vs-body** distinction for a `modified` symbol in `diff`; `moved` detection.
- **Cognitive complexity** (Sonar-style) and fan-in/fan-out alongside cyclomatic.
- **Incremental parsing** — persist Tree-sitter trees keyed by content hash under
  `target/.astcache`; re-parse only changed files. Sub-millisecond on a warm cache.
- **Query pass-through** — accept a Tree-sitter query (`.scm`) and return captures, so
  a repo can add its own structural lints without a new binary.
- **More grammars** — Java, C/C++, C#, Ruby, PHP, Kotlin, Swift, Bash, SQL, HCL.
- **Editor-grade errors** — recovery hints ("expected `)` to match line N").

---

## 2. `zecor-sandbox` — process isolation for lane execution

**Incumbents.** `bubblewrap`/`firejail` (Linux only, config-heavy), `nsjail` (Google,
powerful, gnarly to embed), Docker (heavy, needs a daemon), `sandbox-exec` (macOS,
deprecated but functional, SBPL is undocumented), gVisor (overkill for a test run).
Nothing small, embeddable, and cross-platform with one policy format.

**Gaps.** LLM-coding harnesses run a repo's own test suite — attacker-influenced code —
with the operator's full filesystem and network. The mitigations that exist are
Linux-only and imperative. There is no *declarative* "this command may read the
worktree, write `target/`, and talk to nothing" that works on all three OSes.

**Shipped.** One declarative policy (`allow_read` / `allow_write` / `allow_net` default
deny / env allow-list / cpu-mem-wall-pids caps) with three backends: **Linux** Landlock
LSM + network namespace via `unshare` + rlimits + `PR_SET_NO_NEW_PRIVS`; **macOS** a
generated SBPL profile under `sandbox-exec` + rlimits; **Windows** a Job Object
(kill-on-close, active-process + memory limits); plus a portable rlimit + env-scrub +
chdir fallback. Wall-clock cap kills a hang (exit 124). **`--dry-run`** prints the
resolved plan + policy as JSON without executing. **`--audit FILE`** writes a JSON
record (policy, backend, exit code, timed_out, timing) after the run. Wired into
`workers.base.run_verify` and exposed as `zecor sandbox {run,show}`.

**Still to world-class.**
- **seccomp-bpf** syscall filter on Linux (deny `ptrace`/`mount`/`bpf`/`kexec_load`/
  module ops/…). Needs a Linux dev+CI loop to land safely.
- **cgroup v2** resource caps on Linux; **restricted token / AppContainer** on Windows.
- **Escape telemetry.** Report *which* rule denied *what* (path, syscall) in the audit
  record so a false positive in a repo's gate is diagnosable.
- **Nesting-safe.** Detect an existing sandbox/container and compose rather than fail.

---

## 3. `zecor-tokcount` — token accounting and context control

**Incumbents.** `tiktoken` (OpenAI encodings, Python), HF `tokenizers` (exact per-model,
Rust core but usually driven from Python), `llama.cpp`'s tokenizer (C, model-coupled),
a hundred `chars/4` guesses in every agent framework.

**Gaps.** Nobody ships a single fast binary that: counts against *both* the vendor
billing encoding and a local model's exact tokenizer; trims to a hard budget on a token
boundary and decodes back; **packs** a set of documents into a budget by priority; and
prices the result. Agent frameworks over-truncate (lose the answer) or over-send (pay
for nothing) because the estimate is a guess.

**Shipped.** `count` (tiktoken `cl100k`/`o200k`/`p50k`/`r50k` embedded, or an HF
`tokenizer.json` path) and `trim` (token-boundary truncate + decode). **`cost`** — USD
from a built-in per-model price table (longest-prefix match, so `gpt-4o-2024-08-06`
resolves), `--in-price` / `--out-price` override, `--in-tokens` or stdin. **`pack`** —
greedily assemble files in priority order into a token budget, whole files only, later
smaller files still get a slot; prints the blob, a JSON-ish summary to stderr.
**`diff-trim`** — trim a unified diff to a budget by dropping *whole hunks* from the end
(file headers always kept, a trailing marker records the count). Mirrored in
`zecor.tokens`; the review path (`orchestrator._land_review`) now `diff_trim`s to 60k
tokens before the model sees the diff, and `zecor cost` prices a file/stdin.

**Still to world-class.**
- **Per-doc trim in `pack`** — currently whole-file only; a required-first pass plus a
  fractional trim of the last doc would pack tighter.
- **Streaming count** — read stdin incrementally, stop early past `--budget`.
- **Special-token / chat-template overhead** counting per named template.
- **Offline model registry** — bundle common `tokenizer.json` hashes so
  `--model qwen3-coder` resolves without a path.

---

## 4. `zecor-secretscan` — secret and injection scanning

**Incumbents.** `gitleaks` (Go, the de-facto standard, ~150 rules, regex-only),
`trufflehog` (Go, adds *live credential verification* — its differentiator — but heavy),
`detect-secrets` (Python, entropy + plugins, Yelp), `ripsecrets` (Rust, fast, thin
rule set). GitHub's own scanning is cloud-only.

**Gaps.** `gitleaks` has no entropy tier and no SIMD; `trufflehog`'s verification is
slow and phones home; none of them are built to scan a *generated diff plus the PR text
about to be posted* in the same pass, and none look for *prompt-injection* payloads,
which is a real vector when the diff came from a model fed untrusted input.

**Shipped.** aho-corasick pre-filter → SIMD regex over ~22 credential shapes (AWS/GitHub/
GitLab/Slack/Google/GCP/OpenAI/Anthropic/HF/Stripe/Twilio/SendGrid/npm/PyPI/JWT/DB-URI/
PEM/…) + a Shannon-entropy tier, placeholder allow-list applied per-token, diff-aware
(added lines only, correct file/line), masked hints (never the secret). **Gitignore-aware
directory scan** (`path DIR`, `ignore`-crate semantics, Rayon across files, binary +
oversize skip). **Baseline** — line-independent FNV fingerprint per finding, `baseline
DIR` emits the accept-list, `--baseline FILE` suppresses; a new finding fails, a known one
does not. **SARIF 2.1.0** output (`--format sarif`) with `partialFingerprints`, plus
JSON and text. **Injection pack** (`--injection`) — "ignore previous instructions",
fenced `system:`/`assistant:` blocks, exfil phrasing, tool-use coercion. Mirrored in
`zecor.secretscan` (parity-tested) and exposed as `zecor scan [--dir] [--sarif]
[--injection]`.

**Still to world-class.**
- **Full `gitleaks` rule import.** Load a `gitleaks.toml` so anyone's ruleset works.
- **Verification hooks (opt-in, off by default).** A `--verify` mode that checks a
  candidate against the issuing service *only when explicitly enabled* — trufflehog's
  value without trufflehog's default-on network.
- **Structured entropy.** Per-context floors (a base64 blob in a test fixture vs. in
  `config.py`), and `--entropy N` tuning.
- **Zero-width / RTL-override / base64-payload** detection in the injection pack.
- **Pre-commit + pre-push hook installers.**

---

## 5. `zecor-gitengine` — in-process git

**Incumbents.** `libgit2`/`git2-rs` (C, mature, but a C dep and not fully thread-safe),
`gitoxide`/`gix` (pure Rust, fast, thread-safe, the clear future), shelling out to `git`
(what every agent does — thousands of forks a night, each ~5-15ms of process overhead).

**Gaps.** Agents call `git status`, `git diff`, `git rev-parse`, `git merge-base`,
`git apply`, `git worktree` constantly. Each is a fork+exec+parse. `gix` makes all of
these in-process µs-scale calls, but nobody has packaged the *specific set an agent
needs* behind a stable JSON CLI.

**Shipped.** `head`, `rev-parse`, `merge-base`, `changed-files` (tree/tree), and
`branch-state` (ahead/behind, is-merged, is-descendant, merge-base as one call -- the
auto-merge gate's check). **`log [rev] --limit N`** -- sha, summary, author, epoch time,
parents, newest first. **`status`** -- staged (HEAD vs index, by blob id), unstaged
(index vs worktree via `gix` status), untracked; `is_clean`. All read-only. Mirrored in
`zecor.gitengine` with a `git` fallback; exposed as `zecor git {log,status,...}`.

**Still to world-class.**
- **`diff <a> <b>`** — unified text + a structured hunk list; binary-aware; index/worktree.
- **`apply <patch>`** — three-way patch application with conflict reporting, no
  worktree mutation unless `--write`.
- **`blame <file> --range L1,L2`** — "which symbol/commit does this line trace to".
- **`worktree add/remove/list`** — lane setup without a subshell.
- **Rename detection** in `status` and `changed-files`.
- **`commit-graph` cache** — warm `gix` object DB, shared read handle across lanes.

---

## Cross-cutting standards

- **Cross-platform.** Every crate builds and its tests pass on Linux, macOS, and
  Windows (`x86_64` + `aarch64`); each crate's own repo carries the CI matrix and the
  release workflow that publishes those binaries.
- **No panics on bad input.** A malformed file, diff, or patch is a structured error,
  never a crash.
- **Reproducible.** Same input, same bytes out. No timestamps, no ordering by hashmap.
- **Small.** Release binaries stripped, LTO, `codegen-units = 1`.

A `wasm32-wasip1` build (for an in-process, no-CGO embed) was explored and shelved: two
of the five crates carry C dependencies (`oniguruma`, tree-sitter grammars) that don't
cross that target cleanly. Native binaries via `_rustbridge.py`, called through a pure-
Python fallback at every call site, is the shipped design.
