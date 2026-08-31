# SPDX-License-Identifier: Apache-2.0
"""T5 -- metered OpenAI (gpt-5.x luna), opt-in, ordinary HTTP. $0.20 in / $1.20 out per
1M, capped by a hard monthly spend limit set in the OpenAI dashboard and re-checked in
route() before every call. Long-context recall collapses to 41.3% (vs 91.5% for Sol) --
fine on an 8K brief, useless on a large codebase."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from zecor.workers.base import Job, RateLimit, WorkerResult

ENDPOINT = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5.6-luna"


class LunaWorker:
    tier_name = "T5"

    def __init__(self, api_key_env: str = "OPENAI_METERED_API_KEY") -> None:
        self.api_key_env = api_key_env

    def run(self, job: Job) -> WorkerResult:
        key = os.environ.get(self.api_key_env)
        if not key:
            return WorkerResult("refused", stderr=f"{self.api_key_env} not set (T5 is opt-in)")

        payload = json.dumps(
            {
                "model": job.model or DEFAULT_MODEL,
                "messages": [{"role": "user", "content": job.prompt}],
            }
        ).encode()
        req = urllib.request.Request(
            ENDPOINT,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            # req targets the fixed ENDPOINT constant, never external input
            with urllib.request.urlopen(req, timeout=job.timeout_s) as resp:  # nosemgrep
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code == 429:
                return WorkerResult(
                    "ratelimited", stderr=detail, rate_limit=RateLimit.rolling_5h("openai-metered")
                )
            if e.code in (401, 403):
                return WorkerResult("refused", stderr=f"HTTP {e.code}: {detail}")
            return WorkerResult("fail", stderr=f"HTTP {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            return WorkerResult("timeout", stderr=str(e))

        text = _first_choice(body)
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        return WorkerResult(
            "pass" if text else "fail",
            stdout=text,
            in_tokens=int(usage.get("prompt_tokens", 0)),
            out_tokens=int(usage.get("completion_tokens", 0)),
        )


def _first_choice(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message", {})
    return str(msg.get("content", "")) if isinstance(msg, dict) else ""