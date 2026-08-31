# SPDX-License-Identifier: Apache-2.0
"""`zecor remote setup` -- the low-effort phone path.

Tailscale (through carrier NAT via DERP relays) + mosh (survives lost signal) + tmux
(survives the disconnect). This command checks those are in place, walks the one
interactive step (`tailscale up`), and prints the connection string plus a QR to scan.
Changes nothing by default -- it prints the `brew install` commands; `--install` runs
them (not `tailscale up`, which needs a browser).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

TMUX_SESSION = "cc"


@dataclass(frozen=True)
class RemoteStatus:
    tailscale: bool
    mosh: bool
    tmux: bool
    up: bool               # `tailscale up` completed -- we have a tailnet identity
    dns_name: str          # this machine's MagicDNS name, or ""
    ipv4: str


def _which(c: str) -> bool:
    return shutil.which(c) is not None


def _run(argv: list[str], timeout: float = 15.0) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout + p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def status() -> RemoteStatus:
    ts, mosh, tmux = _which("tailscale"), _which("mosh"), _which("tmux")
    up, dns, ip4 = False, "", ""
    if ts:
        rc, out = _run(["tailscale", "status", "--json"])
        if rc == 0:
            try:
                data = json.loads(out)
                if not isinstance(data, dict):
                    raise TypeError("status is not an object")
                up = data.get("BackendState") == "Running"
                raw_self = data.get("Self")
                self_: dict[str, object] = raw_self if isinstance(raw_self, dict) else {}
                dns = str(self_.get("DNSName") or "").rstrip(".")
                ips = self_.get("TailscaleIPs")
                ips = ips if isinstance(ips, list) else []
                ip4 = next((x for x in ips if isinstance(x, str) and ":" not in x), "")
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
    return RemoteStatus(ts, mosh, tmux, up, dns, ip4)


def connection_string(host: str) -> str:
    """`mosh <host> -- tmux new -A -s cc 'zecor console'` -- attach or create in one go."""
    return f"mosh {host} -- tmux new -A -s {TMUX_SESSION} 'zecor console'"


def _qr(text: str) -> str | None:
    """An ANSI QR via `qrencode` if it is installed -- optional, never required."""
    if not _which("qrencode"):
        return None
    rc, out = _run(["qrencode", "-t", "ANSIUTF8", "-m", "1", text])
    return out if rc == 0 else None


def plan(*, do_install: bool) -> tuple[str, int]:
    """(text, exit code). 0 when the phone recipe is ready to use."""
    st = status()
    lines: list[str] = []
    todo: list[str] = []

    def need(label: str, ok: bool, fix: str) -> None:
        lines.append(f"  {'ok  ' if ok else 'MISS'}  {label}")
        if not ok:
            todo.append(fix)

    need("tailscale", st.tailscale, "brew install tailscale")
    need("mosh", st.mosh, "brew install mosh")
    need("tmux", st.tmux, "brew install tmux")

    if do_install and todo:
        installs = [t for t in todo if t.startswith("brew install")]
        if installs and _which("brew"):
            pkgs = [t.split()[-1] for t in installs]
            lines.append(f"\n  running: brew install {' '.join(pkgs)}")
            rc, out = _run(["brew", "install", *pkgs], timeout=600)
            lines.append("  " + ("done" if rc == 0 else f"brew failed:\n{out[-400:]}"))
            st = status()
            todo = [t for t in todo if not t.startswith("brew install")]

    if not (st.tailscale and st.mosh and st.tmux):
        lines.append("\nInstall the missing pieces, then re-run `zecor remote setup`:")
        lines += [f"  {t}" for t in todo]
        return "\n".join(lines), 1

    if not st.up:
        daemon_down = "failed to connect" in _run(["tailscale", "status"])[1].lower()
        lines.append("\nTailscale is installed but the backend is down. Run:")
        if daemon_down:
            lines.append("  sudo brew services start tailscale   # start the daemon")
        lines.append("  sudo tailscale up                    # complete the browser sign-in")
        lines.append("Then re-run `zecor remote setup` (or `-y` to do it for you).")
        return "\n".join(lines), 1

    host = st.dns_name or st.ipv4 or "<this-machine>.<tailnet>.ts.net"
    conn = connection_string(host)
    lines.append(f"\nMagicDNS name: {host}")
    lines.append("\nOn the phone: install the Tailscale app + a terminal (Termux on")
    lines.append("Android, Blink on iOS), join the same tailnet, then run:")
    lines.append(f"\n  {conn}\n")
    qr = _qr(conn)
    if qr:
        lines.append(qr)
    else:
        lines.append("(install `qrencode` for a scannable QR of that line)")
    lines.append("Losing signal or backgrounding the app is fine -- mosh + tmux hold "
                 "the session; the console itself loses nothing (the transcript is on disk).")
    return "\n".join(lines), 0