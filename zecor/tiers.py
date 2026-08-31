# SPDX-License-Identifier: Apache-2.0
"""The escalation tiers. A tier is a *rate-limited agent with its own refill clock*, not
an HTTP endpoint -- T2b/T3/T4 are driven as subprocesses (vendor CLIs), T2a/T5 are
ordinary HTTP."""

from __future__ import annotations

import enum


class Tier(enum.Enum):
    T0 = "t0"  # tsc / eslint --fix / ast-grep / codemods / the test suite -- electricity only
    T1 = "t1"  # local model on the M1 Max (aider + LM Studio)
    T2A = "t2a"  # Gemini API free tier (AI Studio key, Flash) -- 1500 req/day
    T2B = "t2b"  # Antigravity CLI `agy` (Google AI Pro) -- hard weekly cap
    T3 = "t3"  # Codex CLI (ChatGPT Plus) -- 5h rolling + weekly credit pool
    T4 = "t4"  # Claude Code (Pro) -- tightest budget; Opus+Sonnet share one pool
    T5 = "t5"  # metered OpenAI (gpt-5.x luna), opt-in, hard monthly cap

    NEEDS_HUMAN = "needs_human"  # -- judgment work, never runs unattended
    DEFER = "defer"  # every usable tier dry -- requeue for the next window, do not fail


# Which tiers are a subprocess vs. an HTTP call.
SUBPROCESS_TIERS = frozenset({Tier.T1, Tier.T2B, Tier.T3, Tier.T4})
HTTP_TIERS = frozenset({Tier.T2A, Tier.T5})

# Tiers a task can actually be dispatched to (excludes control outcomes).
DISPATCHABLE = frozenset({Tier.T0, Tier.T1, Tier.T2A, Tier.T2B, Tier.T3, Tier.T4, Tier.T5})

# Independent refill pools: exhausting one never stops another.
VENDOR_OF: dict[Tier, str] = {
    Tier.T2A: "google-free",
    Tier.T2B: "google-aipro",
    Tier.T3: "openai-plus",
    Tier.T4: "anthropic-pro",
    Tier.T5: "openai-metered",
}