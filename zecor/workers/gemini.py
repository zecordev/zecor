# SPDX-License-Identifier: Apache-2.0
"""T2a -- Gemini via an AI Studio API key, ordinary HTTP (not a subprocess). The free
tier is ~1,500 req/day at 15 RPM on Flash-class models with a 1M-context window, in a
quota pool separate from every subscription. Zecor's bulk tier: log squashing, doc/code
drift sweeps, batch classification, first-pass review of mechanical diffs.

Env:
  GEMINI_API_KEY (or GOOGLE_API_KEY)   the AI Studio key
  ZECOR_GEMINI_MODEL                    default: gemini-flash-latest -- point it at a
                                         Pro model only if the key has billing enabled

This worker reads/writes no files; it returns text.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from zecor.workers.base import Job, RateLimit, WorkerResult

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-flash-latest"
_KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


class GeminiWorker:
    tier_name = "T2a"

    def __init__(self, key_envs: tuple[str, ...] = _KEY_ENVS) -> None:
        self.key_envs = key_envs

    def _key(self) -> str | None:
        for name in self.key_envs:
            v = os.environ.get(name)
            if v:
                return v
        return None

    def run(self, job: Job) -> WorkerResult:
        key = self._key()
        if not key:
            return WorkerResult("refused", stderr=f"none of {self.key_envs} is set")

        model = job.model or os.environ.get("ZECOR_GEMINI_MODEL") or DEFAULT_MODEL
        url = ENDPOINT.format(model=model)
        payload = json.dumps({"contents": [{"parts": [{"text": job.prompt}]}]}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        body = None
        for attempt in range(3):
            try:
                # ENDPOINT is the fixed Gemini API host; only the model slug is
                # interpolated in
                with urllib.request.urlopen(req, timeout=job.timeout_s) as resp:  # nosemgrep
                    body = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                if e.code == 429:
                    return WorkerResult(
                        "ratelimited", stderr=detail, rate_limit=RateLimit.daily("google-free")
                    )
                if e.code in (500, 502, 503, 504):
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
                        continue
                    return WorkerResult("timeout", stderr=f"HTTP {e.code} after 3 tries: {detail}")
                # 400 / 401 / 403 / 404 -- not our problem to retry
                return WorkerResult("refused", stderr=f"HTTP {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError) as e:
                return WorkerResult("timeout", stderr=str(e))
        if body is None:
            return WorkerResult("timeout", stderr="no response")

        text = _first_text(body)
        usage = body.get("usageMetadata", {}) if isinstance(body, dict) else {}
        return WorkerResult(
            "pass" if text else "fail",
            stdout=text,
            in_tokens=int(usage.get("promptTokenCount", 0)),
            out_tokens=int(usage.get("candidatesTokenCount", 0)),
        )


def _first_text(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    cands = body.get("candidates")
    if not isinstance(cands, list) or not cands:
        return ""
    parts = cands[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))