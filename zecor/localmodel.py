# SPDX-License-Identifier: Apache-2.0
"""Thin client for the local LM Studio endpoint, stdlib only. Used by the volume stages
Zecor keeps off the frontier: intake classification, brief generation, log squashing,
commit subjects, the digest. Every local prompt is capped at 8K tokens in the harness --
a task needing more is sliced wrong, or belongs to T2a."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from zecor.config import LM_BASE_URL, MODEL_EMBED, MODEL_ROUTER
from zecor.route import LOCAL_PROMPT_CAP

_CHARS_PER_TOKEN = 4
# LM Studio drops the keep-alive connection when it swaps models or the machine wakes;
# retry a couple of times with backoff before treating the attempt as lost.
_RETRIES = 3
_BACKOFF_S = (0.0, 1.5, 4.0)


def _post_json(url: str, body: dict[str, object] | None, timeout_s: float) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    last: Exception | None = None
    for i in range(_RETRIES):
        if i:
            time.sleep(_BACKOFF_S[min(i, len(_BACKOFF_S) - 1)])
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            # url is LM_BASE_URL, the operator's own local-model endpoint from
            # config/env, never attacker input
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosemgrep
                parsed = json.loads(resp.read())
            return parsed if isinstance(parsed, dict) else {"_": parsed}
        except urllib.error.HTTPError as e:
            raise LocalModelError(f"HTTP {e.code}: {e.reason}") from e  # a real 4xx/5xx
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e  # a dropped socket / refused connection / timeout -- retry
        except json.JSONDecodeError as e:
            last = e
            break  # a 200 with a garbage body won't get better on retry
    raise LocalModelError(str(last))


class LocalModelError(RuntimeError):
    """The local endpoint was unreachable or refused -- the caller decides fallback."""


class PromptTooLong(ValueError):
    """Over the 8K cap. Slice the task or route to T2a -- do not truncate silently."""


@dataclass
class Completion:
    text: str
    in_tokens: int
    out_tokens: int


def complete(
    prompt: str,
    *,
    model: str = MODEL_ROUTER,
    system: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    timeout_s: float = 120.0,
    json_schema: dict[str, object] | None = None,
) -> Completion:
    from zecor import tokens

    est = tokens.count(prompt) if tokens.exact_available() else len(prompt) // _CHARS_PER_TOKEN
    if est > LOCAL_PROMPT_CAP:
        raise PromptTooLong(f"~{est} tokens > {LOCAL_PROMPT_CAP} cap")

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, object] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if json_schema is not None:
        # LM Studio structured output -- makes a malformed brief impossible.
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "zecor", "strict": True, "schema": json_schema},
        }

    data = _post_json(f"{LM_BASE_URL}/chat/completions", body, timeout_s)

    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise LocalModelError(f"unexpected response shape: {data}") from e
    usage = data.get("usage", {}) if isinstance(data, dict) else {}
    return Completion(
        text=text,
        in_tokens=int(usage.get("prompt_tokens", 0)),
        out_tokens=int(usage.get("completion_tokens", 0)),
    )


def available() -> bool:
    try:
        # LM_BASE_URL is the operator's own local-model endpoint from config/env
        with urllib.request.urlopen(f"{LM_BASE_URL}/models", timeout=5) as resp:  # nosemgrep
            data = json.loads(resp.read())
        return bool(data.get("data"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False


def embed(
    texts: list[str], *, model: str = MODEL_EMBED, timeout_s: float = 60.0
) -> list[list[float]]:
    """Embed a batch of texts via LM Studio. Raises LocalModelError on any failure --
    the caller (retrieval) treats embeddings as optional and falls back to lexical."""
    if not texts:
        return []
    data = _post_json(
        f"{LM_BASE_URL}/embeddings", {"model": model, "input": texts}, timeout_s
    )
    try:
        return [list(map(float, row["embedding"])) for row in data["data"]]
    except (KeyError, TypeError) as e:
        raise LocalModelError(str(e)) from e