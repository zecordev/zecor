# SPDX-License-Identifier: Apache-2.0
"""System-resource governor. Zecor shares one laptop with LM Studio (~32 GB of models
resident) and, during a build-out session, a fan-out of review agents -- two local-model
consumers at once thrash the GPU. `snapshot()` reads the machine cheaply and
`can_start()`/`advise_lanes()` turn that into a go/no-go, degrading safe when a probe
fails.

Tunables (env):
  ZECOR_RAM_FLOOR_GB      hard floor of free RAM before any new local work  (default 6)
  ZECOR_RAM_FLOOR_LIGHT_GB floor for a cloud-backed subprocess              (default 3)
  ZECOR_LOCAL_MODEL_MAX   CEILING on concurrent local-model consumers       (default 3)
  ZECOR_BIG_MODEL_GB      at/above this resident size, one consumer at a time (default 12)
  ZECOR_KV_HEADROOM_GB    working set assumed on top of resident weights    (default 3)
  ZECOR_LOAD_FACTOR       load-average ceiling as a fraction of cpu count   (default 0.9)
  ZECOR_MAX_LANES         hard cap advise_lanes() will never exceed         (default 2)
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PAGE_RE = re.compile(r"page size of (\d+) bytes")
FREE_RE = re.compile(r"Pages free:\s+(\d+)")
INACTIVE_RE = re.compile(r"Pages inactive:\s+(\d+)")
SPEC_RE = re.compile(r"Pages speculative:\s+(\d+)")
PURGE_RE = re.compile(r"Pages purgeable:\s+(\d+)")
SWAP_USED_RE = re.compile(r"used = ([\d.]+)M")


def _f(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default
    return v if math.isfinite(v) else default  # nan/inf would silently disable a gate


def _i(name: str, default: int) -> int:
    v = _f(name, float(default))
    try:
        return int(v)
    except (ValueError, OverflowError):
        return default


RAM_FLOOR_GB = _f("ZECOR_RAM_FLOOR_GB", 6.0)
RAM_FLOOR_LIGHT_GB = _f("ZECOR_RAM_FLOOR_LIGHT_GB", 3.0)
LOCAL_MODEL_MAX = max(1, _i("ZECOR_LOCAL_MODEL_MAX", 3))  # a CEILING now, not the value
LOAD_FACTOR = _f("ZECOR_LOAD_FACTOR", 0.9)
MAX_LANES = max(1, _i("ZECOR_MAX_LANES", 2))
SWAP_CEILING_GB = _f("ZECOR_SWAP_CEILING_GB", 4.0)
BIG_MODEL_GB = _f("ZECOR_BIG_MODEL_GB", 12.0)  # at/above this, one concurrent consumer
KV_HEADROOM_GB = _f("ZECOR_KV_HEADROOM_GB", 3.0)  # working set on top of resident weights

# What a `kind` needs. "local" means it competes for LM Studio / the GPU.
LOCAL_KINDS = frozenset({"local_lane", "local_model", "local_review", "aider"})
LIGHT_KINDS = frozenset(
    {"subprocess_worker", "frontier_review", "frontier", "test_run", "cloud_agent"}
)


@dataclass
class ResourceSnapshot:
    ram_free_gb: float  # free + inactive + speculative + purgeable -- reclaimable now
    ram_total_gb: float
    swap_used_gb: float
    load1: float
    cpu_count: int
    disk_free_gb: float
    lmstudio_models: list[str] = field(default_factory=list)
    lmstudio_vram_gb: float = 0.0
    lmstudio_sizes: dict[str, float] = field(default_factory=dict)
    aider_jobs: int = 0  # aider processes -- a lane doing patch work
    zecor_runs: int = 0  # `zecor run` processes -- brief/lens/intake HTTP to LM Studio
    permit_holders: int = 0  # entries in the permit registry -- the authoritative count
    permit_gbs: list[float] = field(default_factory=list)  # each held permit's model footprint
    probe_errors: list[str] = field(default_factory=list)

    @property
    def local_consumers(self) -> int:
        # permit registry is the source of truth for zecor-owned work; a stray aider
        # started outside Zecor still shows via pgrep.
        return max(self.permit_holders, self.aider_jobs)

    @property
    def load_ratio(self) -> float:
        return self.load1 / self.cpu_count if self.cpu_count else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "ram_free_gb": round(self.ram_free_gb, 1),
            "ram_total_gb": round(self.ram_total_gb, 1),
            "swap_used_gb": round(self.swap_used_gb, 2),
            "load1": round(self.load1, 2),
            "cpu_count": self.cpu_count,
            "load_ratio": round(self.load_ratio, 2),
            "disk_free_gb": round(self.disk_free_gb, 1),
            "lmstudio_models": self.lmstudio_models,
            "lmstudio_vram_gb": round(self.lmstudio_vram_gb, 1),
            "lmstudio_sizes": {k: round(v, 1) for k, v in self.lmstudio_sizes.items()},
            "aider_jobs": self.aider_jobs,
            "zecor_runs": self.zecor_runs,
            "permit_holders": self.permit_holders,
            "local_consumers": self.local_consumers,
            "probe_errors": self.probe_errors,
        }


def _run(cmd: list[str], timeout: float = 4.0) -> str:
    """Best-effort probe. A hang, a missing binary, or a crash all yield "" -- the
    caller must never see an exception (that would defeat the whole fail-safe design)."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _mem() -> tuple[float, float, float, list[str]]:
    """(free_gb, total_gb, swap_used_gb, errors)."""
    if sys.platform == "linux":
        return _mem_linux()
    return _mem_macos()


def _mem_linux() -> tuple[float, float, float, list[str]]:
    """/proc/meminfo, in kB. MemAvailable is the kernel's own free-without-swapping
    estimate -- the same intent as macOS's free+inactive+speculative+purgeable.
    Read via `_run` (not a direct file read) so a probe failure is injectable the
    same way as every other probe here."""
    errs: list[str] = []
    fields: dict[str, float] = {}
    raw = _run(["cat", "/proc/meminfo"])
    if not raw:
        errs.append("/proc/meminfo: unreadable")
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        m = re.match(r"\s*(\d+)", rest)
        if m:
            fields[key] = int(m.group(1)) / 1024**2  # kB -> GB
    total_gb = fields.get("MemTotal", 0.0)
    free_gb = fields.get("MemAvailable", 0.0)
    if "MemAvailable" not in fields:
        errs.append("MemAvailable: not reported")
        free_gb = max(total_gb - 52.0, 2.0)  # same pessimistic fallback as macOS
    swap_total = fields.get("SwapTotal", 0.0)
    swap_free = fields.get("SwapFree", 0.0)
    swap_gb = max(swap_total - swap_free, 0.0)
    return free_gb, total_gb, swap_gb, errs


def _mem_macos() -> tuple[float, float, float, list[str]]:
    """Free = pages the OS can hand out without swapping: free + inactive +
    speculative + purgeable."""
    errs: list[str] = []
    total_gb = 0.0
    try:
        total_gb = int(_run(["sysctl", "-n", "hw.memsize"]).strip()) / 1024**3
    except (ValueError, OSError) as e:
        errs.append(f"hw.memsize: {e}")
    free_gb = 0.0
    try:
        vs = _run(["vm_stat"])
        page = int(PAGE_RE.search(vs).group(1))  # type: ignore[union-attr]
        pages = 0
        for rx in (FREE_RE, INACTIVE_RE, SPEC_RE, PURGE_RE):
            m = rx.search(vs)
            if m:
                pages += int(m.group(1))
        free_gb = pages * page / 1024**3
    except (AttributeError, ValueError, OSError) as e:
        errs.append(f"vm_stat: {e}")
        # Pessimistic: a probe failure leans toward "no headroom", not "plenty".
        free_gb = max(total_gb - 52.0, 2.0)
    swap_gb = 0.0
    try:
        m = SWAP_USED_RE.search(_run(["sysctl", "vm.swapusage"]))
        if m:
            swap_gb = float(m.group(1)) / 1024
    except (ValueError, OSError) as e:
        errs.append(f"swapusage: {e}")
    return free_gb, total_gb, swap_gb, errs


def _lmstudio() -> tuple[list[str], float, dict[str, float], list[str]]:
    lms = shutil.which("lms") or str(Path.home() / ".lmstudio" / "bin" / "lms")
    if not Path(lms).exists():
        return [], 0.0, {}, []
    try:
        raw = _run([lms, "ps", "--json"], timeout=6.0)
        data = json.loads(raw) if raw.strip() else []
    except (json.JSONDecodeError, OSError) as e:
        return [], 0.0, {}, [f"lms ps: {e}"]
    models: list[str] = []
    sizes: dict[str, float] = {}
    vram = 0.0
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("modelKey") or row.get("path") or "?")
        models.append(key)
        with contextlib.suppress(TypeError, ValueError):
            gb = float(row.get("sizeBytes") or 0) / 1024**3  # null / non-numeric -> 0
            sizes[key] = gb
            vram += gb
    return models, vram, sizes, []


_AIDER_RE = re.compile(r"(^|/)aider(-chat)?($|\s|/)")
_ZECORRUN_RE = re.compile(r"-m zecor\b.*\brun\b|bin/zecor\b.*\brun\b")


def _local_consumers() -> tuple[int, int]:
    """(aider_jobs, zecor_runs). An `aider` holds the model for patch work; a `zecor run`
    drives briefs/lenses/intake over HTTP and counts even between its model calls, the
    conservative read. Excludes the caller's own process."""
    me = os.getpid()
    lines = (_run(["pgrep", "-fl", "aider"]) + "\n" + _run(["pgrep", "-fl", "zecor"])).splitlines()
    aiders = zecorruns = 0
    seen: set[int] = set()
    for line in lines:
        parts = line.split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid == me or pid in seen:
            continue
        seen.add(pid)
        rest = parts[1] if len(parts) > 1 else ""
        if _AIDER_RE.search(rest.lower()):
            aiders += 1
        elif _ZECORRUN_RE.search(rest):
            zecorruns += 1
    return aiders, zecorruns


def snapshot() -> ResourceSnapshot:
    free_gb, total_gb, swap_gb, mem_errs = _mem()
    models, vram, sizes, lms_errs = _lmstudio()
    aiders, zecorruns = _local_consumers()
    permit_gbs: list[float] = []
    with contextlib.suppress(Exception):  # permits must never break a resource read
        from zecor.permits import held

        for p in held():
            try:
                permit_gbs.append(float(p.get("gb", 12.0)))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                permit_gbs.append(12.0)
    permits = len(permit_gbs)
    try:
        cpu = os.cpu_count() or 8
    except OSError:
        cpu = 8
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    try:
        disk_free_gb = shutil.disk_usage(Path.home()).free / 1024**3
    except OSError:
        disk_free_gb = 999.0
    return ResourceSnapshot(
        ram_free_gb=free_gb,
        ram_total_gb=total_gb,
        swap_used_gb=swap_gb,
        load1=load1,
        cpu_count=cpu,
        disk_free_gb=disk_free_gb,
        lmstudio_models=models,
        lmstudio_vram_gb=vram,
        lmstudio_sizes=sizes,
        aider_jobs=aiders,
        zecor_runs=zecorruns,
        permit_holders=permits,
        permit_gbs=permit_gbs,
        probe_errors=mem_errs + lms_errs,
    )


def can_start(kind: str, *, snap: ResourceSnapshot | None = None) -> tuple[bool, str]:
    """Is there headroom to start one more unit of work of this `kind`? local_lane,
    local_model and local_review compete for the GPU; subprocess_worker, frontier_review,
    test_run and cloud_agent are light. Unknown kinds are treated as light."""
    s = snap or snapshot()
    if s.load_ratio > LOAD_FACTOR * 1.5:
        return False, f"load {s.load1:.1f} on {s.cpu_count} cores -- way over"
    if s.swap_used_gb > SWAP_CEILING_GB:
        return False, f"swap {s.swap_used_gb:.1f}GB used -- over the {SWAP_CEILING_GB}GB ceiling"

    if kind in LOCAL_KINDS:
        big_held = [g for g in s.permit_gbs if g >= BIG_MODEL_GB]
        small_kind = kind == "local_model"  # only the 4B router tier is "small"
        if s.local_consumers >= LOCAL_MODEL_MAX:
            return (
                False,
                f"{s.local_consumers} local-model consumer(s) held (ceiling {LOCAL_MODEL_MAX})",
            )
        if not small_kind and big_held:
            return False, f"a big-model consumer ({big_held[0]:.0f}GB) is already running"
        if s.ram_free_gb < RAM_FLOOR_GB:
            return False, f"{s.ram_free_gb:.1f}GB free < {RAM_FLOOR_GB}GB floor for local work"
        if s.load_ratio > LOAD_FACTOR:
            return False, f"load ratio {s.load_ratio:.2f} > {LOAD_FACTOR} -- let it settle"
        return True, "ok for local-model work"

    # light / cloud-backed
    if s.ram_free_gb < RAM_FLOOR_LIGHT_GB:
        return False, f"{s.ram_free_gb:.1f}GB free < {RAM_FLOOR_LIGHT_GB}GB floor"
    return True, "ok for a cloud-backed / cpu job"


def advise_lanes(*, snap: ResourceSnapshot | None = None) -> int:
    """How many concurrent lanes `zecor run` should use right now (1..MAX_LANES)."""
    s = snap or snapshot()
    tight = (
        s.ram_free_gb < RAM_FLOOR_GB
        or s.load_ratio > LOAD_FACTOR
        or s.swap_used_gb > SWAP_CEILING_GB
    )
    if tight:
        return 1
    # A second lane means a second aider against the same resident model -- thrashes the
    # GPU -- so only allow it when none is already running and RAM has a model to spare.
    if (
        s.local_consumers == 0
        and s.ram_free_gb >= RAM_FLOOR_GB + 12
        and s.load_ratio < LOAD_FACTOR * 0.6
    ):
        return min(MAX_LANES, 2)
    return 1