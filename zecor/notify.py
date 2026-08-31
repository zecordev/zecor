# SPDX-License-Identifier: Apache-2.0
"""Reach the operator when they are not looking -- Console II "louder failure surfacing".

Every notification appends a `notice` transcript turn (so the console shows it whenever
next opened) and, if a push channel is configured, fires that too -- best-effort, every
error swallowed, since `notify()` is called from a health tick and the supervisor and
neither may stall on a bad network. Channels are opt-in by env, no dependency beyond
`urllib`: **ntfy** (`ZECOR_NTFY_TOPIC` or `ZECOR_NTFY_URL`) and **Pushover**
(`ZECOR_PUSHOVER_TOKEN` / `ZECOR_PUSHOVER_USER`).
"""

from __future__ import annotations

import contextlib
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

LEVELS = ("info", "warn", "crit")
_NTFY_PRIORITY = {"info": "3", "warn": "4", "crit": "5"}
_PUSHOVER_PRIORITY = {"info": "-1", "warn": "0", "crit": "1"}
_TIMEOUT_S = 6.0


@dataclass(frozen=True)
class NotifyResult:
    transcript_id: int
    channels: list[str] = field(default_factory=list)  # which push channels accepted it
    errors: list[str] = field(default_factory=list)


def _ntfy_target() -> str | None:
    url = os.environ.get("ZECOR_NTFY_URL", "").strip()
    if url:
        return url.rstrip("/")
    topic = os.environ.get("ZECOR_NTFY_TOPIC", "").strip()
    return f"https://ntfy.sh/{topic}" if topic else None


def _post(req: urllib.request.Request) -> None:
    # req targets a fixed vendor endpoint (ntfy/Pushover), never external input
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # nosemgrep
        resp.read()


def _send_ntfy(title: str, body: str, level: str) -> None:
    target = _ntfy_target()
    if not target:
        raise _Skip
    req = urllib.request.Request(
        target,
        data=body.encode("utf-8"),
        headers={
            "Title": title[:250],
            "Priority": _NTFY_PRIORITY.get(level, "3"),
            "Tags": {"info": "information_source", "warn": "warning", "crit": "rotating_light"}[
                level if level in LEVELS else "info"
            ],
        },
        method="POST",
    )
    _post(req)


def _send_pushover(title: str, body: str, level: str) -> None:
    tok = os.environ.get("ZECOR_PUSHOVER_TOKEN", "").strip()
    usr = os.environ.get("ZECOR_PUSHOVER_USER", "").strip()
    if not (tok and usr):
        raise _Skip
    payload = urllib.parse.urlencode(
        {
            "token": tok, "user": usr, "title": title[:250], "message": body[:1024],
            "priority": _PUSHOVER_PRIORITY.get(level, "0"),
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json", data=payload, method="POST"
    )
    _post(req)


class _Skip(Exception):
    """Channel not configured -- not an error, just skipped."""


_CHANNELS = (("ntfy", _send_ntfy), ("pushover", _send_pushover))


def notify(
    title: str,
    body: str = "",
    *,
    level: str = "info",
    card_id: str | None = None,
) -> NotifyResult:
    """Record a notice turn, then fan out to any configured push channel. Never raises."""
    from zecor.core import transcript

    level = level if level in LEVELS else "info"
    text = f"{title}\n{body}".strip() if body else title
    tid = 0
    with contextlib.suppress(Exception):  # a broken db must not stop the push
        tid = transcript.append(
            "zecor", "notice", text, card_id=card_id, meta={"level": level}
        )

    sent: list[str] = []
    errs: list[str] = []
    for name, fn in _CHANNELS:
        try:
            fn(title, body or title, level)
            sent.append(name)
        except _Skip:
            continue
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            errs.append(f"{name}: {str(e)[:120]}")
        except Exception as e:  # best-effort: a channel bug is not fatal
            errs.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
    return NotifyResult(tid, sent, errs)


def configured_channels() -> list[str]:
    out = []
    if _ntfy_target():
        out.append("ntfy")
    if os.environ.get("ZECOR_PUSHOVER_TOKEN") and os.environ.get("ZECOR_PUSHOVER_USER"):
        out.append("pushover")
    return out


def selftest() -> NotifyResult:
    """`zecor notify --test`: send a real notification, report per-channel outcome."""
    return notify(
        "zecor notify test",
        "If you can see this on your phone, push is wired.",
        level="info",
    )