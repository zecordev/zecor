# SPDX-License-Identifier: Apache-2.0
"""Token counting and context trimming. `count()` returns an exact BPE count via the
`zecor-tokcount` Rust crate when built, falling back to a conservative byte/char
heuristic otherwise -- the safe direction for a cap. `trim()` cuts a prompt to a hard
token budget."""

from __future__ import annotations

import os
import subprocess

from zecor import _rustbridge

DEFAULT_ENCODING = "cl100k_base"


def _heuristic(text: str) -> int:
    # mean of char and byte counts over 6 -- over-estimates for multibyte, the safe side.
    return (len(text) + len(text.encode("utf-8"))) // 6


def count(text: str, *, encoding: str = DEFAULT_ENCODING) -> int:
    """Exact token count if the Rust helper is built, else a conservative estimate."""
    obj = _rustbridge.run_json("zecor-tokcount", ["count", "--enc", encoding], text)
    if isinstance(obj, dict) and isinstance(obj.get("tokens"), int):
        return int(obj["tokens"])
    return _heuristic(text)


def trim(text: str, max_tokens: int, *, encoding: str = DEFAULT_ENCODING) -> str:
    """Truncate `text` to at most `max_tokens` tokens on a token boundary. Falls back to
    a proportional byte cut when the Rust helper is unavailable."""
    b = _rustbridge.binary("zecor-tokcount")
    if b is not None and max_tokens > 0:
        try:
            p = subprocess.run(
                [b, "trim", "--max", str(max_tokens), "--enc", encoding],
                input=text, capture_output=True, text=True, timeout=30, check=False,
            )
            if p.returncode == 0:
                return p.stdout
        except (OSError, subprocess.SubprocessError):
            pass
    if count(text, encoding=encoding) <= max_tokens:
        return text
    # heuristic: ~ (chars+bytes)/6 tokens -> keep the matching fraction of chars
    keep = max(0, int(len(text) * (max_tokens / max(1, _heuristic(text)))))
    return text[:keep]


def exact_available() -> bool:
    return _rustbridge.binary("zecor-tokcount") is not None and not os.environ.get("ZECOR_NO_RUST")


# Public list prices, USD per 1M tokens (input, output). Mirrors the Rust table; the
# Python side is the fallback and does not need to be exhaustive.
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00), "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00), "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1-nano": (0.10, 0.40),
    "o3": (2.00, 8.00), "o4-mini": (1.10, 4.40),
    "claude-opus-4": (15.00, 75.00), "claude-sonnet-4": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00), "claude-3-haiku": (0.25, 1.25),
    "gemini-1.5-pro": (1.25, 5.00), "gemini-1.5-flash": (0.075, 0.30),
    "gemini-2.0-flash": (0.10, 0.40), "deepseek-chat": (0.27, 1.10), "local": (0.0, 0.0),
}


def price_per_mtok(model: str) -> tuple[float, float] | None:
    m = model.lower()
    hits = [(k, v) for k, v in _PRICE_PER_MTOK.items() if m.startswith(k) or k in m]
    return max(hits, key=lambda kv: len(kv[0]))[1] if hits else None


def cost(
    model: str, in_tokens: int, out_tokens: int = 0,
    *, price: tuple[float, float] | None = None,
) -> dict[str, float | int | str]:
    """USD estimate for a request. Prefers the Rust table; `price` overrides."""
    if price is None:
        obj = _rustbridge.run_json(
            "zecor-tokcount",
            ["cost", "--model", model, "--in-tokens", str(in_tokens),
             "--out-tokens", str(out_tokens)],
            "",
        )
        if isinstance(obj, dict) and "total_usd" in obj:
            return obj
    pin, pout = price or price_per_mtok(model) or (0.0, 0.0)
    iu, ou = in_tokens / 1e6 * pin, out_tokens / 1e6 * pout
    return {"model": model, "in_tokens": in_tokens, "out_tokens": out_tokens,
            "in_usd": iu, "out_usd": ou, "total_usd": iu + ou}


def diff_trim(diff: str, max_tokens: int, *, encoding: str = DEFAULT_ENCODING) -> str:
    """Trim a unified diff to a token budget by dropping whole hunks (file headers kept).
    Falls back to `trim()` on the raw text when the Rust helper is unavailable."""
    b = _rustbridge.binary("zecor-tokcount")
    if b is not None and max_tokens > 0:
        try:
            p = subprocess.run(
                [b, "diff-trim", "--max", str(max_tokens), "--enc", encoding],
                input=diff, capture_output=True, text=True, timeout=30, check=False,
            )
            if p.returncode == 0:
                return p.stdout
        except (OSError, subprocess.SubprocessError):
            pass
    return trim(diff, max_tokens, encoding=encoding)
