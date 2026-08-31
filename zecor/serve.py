# SPDX-License-Identifier: Apache-2.0
"""HTTP shell for the command-center PWA -- stdlib `http.server`, no deps.

Routes:
  GET  /                      the PWA shell (webui/dist if built, else a fallback page)
  GET  /api/state             board + status + open questions + momentum, one call
  GET  /api/transcript?since= conversation turns after an id
  GET  /api/roadmap           the rolled-up tree
  GET  /api/metrics           the ledger summary
  GET  /api/stream            SSE: a tick whenever the transcript or board changes
  POST /api/say               a chat line (grammar first, then Layer-2)
  POST /api/intent            an allow-listed verb {verb, args}

Meant to sit behind Tailscale (`--bind <tailnet-ip> --token <secret>`). The static shell
needs no token (it holds no data); every `/api/*` route requires `?t=<token>` or an
`X-Fleet-Token` / `Authorization: Bearer` header -- not hardened for the open internet,
the tailnet is the perimeter.
"""
# ruff: noqa: E501 -- inline HTML fallback; the column limit is not useful here

from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from zecor import config, webapi
from zecor.core import transcript

_MAX_STREAMS = 8  # concurrent SSE clients; a token holder must not exhaust the pool
_STREAM_MAX_S = 3600.0  # a stream is recycled after this so a wedged client frees a slot
_streams = threading.BoundedSemaphore(_MAX_STREAMS)

_WEBUI_DIST = Path(__file__).resolve().parents[1] / "webui" / "dist"

_FALLBACK = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>zecor console</title>
<style>body{font:15px system-ui,sans-serif;margin:2rem;max-width:40rem;color:#ddd;background:#111}</style>
<h1>zecor console &middot; API is up</h1>
<p>The PWA is not built. Build it once:</p>
<pre>cd webui &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>The JSON API is live now: <code>/api/state</code>, <code>/api/transcript</code>,
<code>/api/roadmap</code>, <code>/api/metrics</code>, <code>/api/stream</code>;
<code>POST /api/say</code>, <code>POST /api/intent</code>. Append <code>?t=&lt;token&gt;</code>.</p>
"""

_CTYPE = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    token: str | None = None

    # -- helpers ----------------------------------------------------------------

    def _authed(self) -> bool:
        if not self.token:
            return True
        q = parse_qs(urlparse(self.path).query)
        if q.get("t", [""])[0] == self.token:
            return True
        if self.headers.get("X-Fleet-Token") == self.token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            self.wfile.write(body)

    def _json(self, obj: object, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict[str, object]:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n) if n else b""
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    # -- GET ------------------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            if not self._authed():
                self._json({"error": "unauthorized"}, 401)
                return
            self._get_api(path)
            return
        self._get_static(path)

    def _get_api(self, path: str) -> None:
        q = parse_qs(urlparse(self.path).query)
        if path == "/api/state":
            self._json(webapi.state())
        elif path == "/api/transcript":
            since = int(q.get("since", ["0"])[0] or 0)
            self._json(webapi.transcript_since(since))
        elif path == "/api/roadmap":
            self._json(webapi.roadmap_tree())
        elif path == "/api/metrics":
            self._json(webapi.metrics_view())
        elif path == "/api/stream":
            self._stream()
        else:
            self._json({"error": "not found"}, 404)

    def _stream(self) -> None:
        if not _streams.acquire(blocking=False):
            self._json({"error": "too many streams"}, 503)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            with contextlib.suppress(OSError):
                self.connection.settimeout(30.0)  # a stalled write frees the slot
            deadline = time.monotonic() + _STREAM_MAX_S
            last_turn = -1
            last_cards = ""
            while time.monotonic() < deadline:
                cur_turn = transcript.latest_id()
                cur_cards = _board_key()
                if cur_turn != last_turn or cur_cards != last_cards:
                    last_turn, last_cards = cur_turn, cur_cards
                    payload = json.dumps({"latest_turn": cur_turn})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")  # keep the socket warm
                self.wfile.flush()
                time.sleep(1.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            _streams.release()

    def _get_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            f = _WEBUI_DIST / "index.html"
            if f.is_file():
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(200, _FALLBACK.encode(), "text/html; charset=utf-8")
            return
        # any other path -> a file under webui/dist, never outside it
        rel = path.lstrip("/")
        target = (_WEBUI_DIST / rel).resolve()
        try:
            target.relative_to(_WEBUI_DIST.resolve())
        except ValueError:
            self._send(403, b"forbidden\n", "text/plain")
            return
        if target.is_file():
            self._send(200, target.read_bytes(), _CTYPE.get(target.suffix, "application/octet-stream"))
        else:
            self._send(404, b"not found\n", "text/plain")

    # -- POST ---------------------------------------------------------------

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._authed():
            self._json({"error": "unauthorized"}, 401)
            return
        body = self._body()
        if path == "/api/say":
            self._json(webapi.say(str(body.get("text", ""))))
        elif path == "/api/intent":
            args = body.get("args")
            self._json(webapi.intent(str(body.get("verb", "")),
                                     args if isinstance(args, dict) else {}))
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *_a: object) -> None:  # quiet
        return


def _board_key() -> str:
    try:
        from zecor.console import model as cmodel
        from zecor.core import view

        return str(cmodel.snapshot_key(view.cards()))
    except Exception:
        return ""


def serve(bind: str, port: int, token: str | None) -> int:
    config.ensure_dirs()
    Handler.token = token
    httpd = ThreadingHTTPServer((bind, port), Handler)
    where = f"http://{bind}:{port}/" + (f"?t={token}" if token else "")
    print(f"zecor serve -> {where}", flush=True)
    if bind in ("127.0.0.1", "localhost") and not token:
        print("  (localhost only; use --bind <tailscale-ip> --token <secret> for remote)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(serve("127.0.0.1", 8787, None))