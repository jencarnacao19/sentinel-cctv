"""System diagnostics.

What the "Sistema → Diagnóstico" page needs to answer one question: *is this
install healthy, and if not, which part is unhappy?* Everything here is read
directly from the running process and the host, never cached, because a stale
diagnostic is worse than none.

Nothing in the output is sensitive. Camera credentials, tokens and RTSP userinfo
are excluded by construction: this module only ever reports counts, states and
resource figures, and everything it does emit passes through redaction before it
leaves the process.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from app import __version__
from app.config import Settings
from app.logging_config import get_logger
from app.security.redaction import redact_mapping

logger = get_logger("SYSTEM")

#: Sampling window for the process-wide CPU figure. psutil needs two readings to
#: produce a meaningful percentage; anything shorter is noise.
CPU_SAMPLE_SECONDS = 0.3


@dataclass(frozen=True, slots=True)
class HostMetrics:
    cpu_percent: float
    cpu_count: int
    load_average: tuple[float, float, float] | None
    """Unavailable on Windows, so it is explicitly nullable rather than faked."""
    memory_total_bytes: int
    memory_used_bytes: int
    memory_percent: float
    swap_used_bytes: int
    boot_time: datetime | None


@dataclass(frozen=True, slots=True)
class ProcessMetrics:
    pid: int
    cpu_percent: float
    memory_rss_bytes: int
    threads: int
    open_files: int | None
    """None when the platform or permissions do not allow counting them."""
    children: int
    ffmpeg_children: int
    """The recorders and motion readers. A count far from what is expected is the
    clearest single signal that something is wrong."""


@dataclass(frozen=True, slots=True)
class DiskMetrics:
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    available: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Diagnostics:
    version: str
    generated_at: datetime
    python_version: str
    platform: str
    container: bool
    host: HostMetrics
    process: ProcessMetrics
    disks: list[DiskMetrics]
    tooling: dict[str, str | None]
    subsystems: dict[str, Any] = field(default_factory=dict)


def _load_average() -> tuple[float, float, float] | None:
    # getloadavg exists on Windows in recent CPython but returns a simulated
    # value; reporting None is more honest than a number that means nothing.
    if not hasattr(os, "getloadavg") or os.name == "nt":
        return None
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):
        return None
    return (round(one, 2), round(five, 2), round(fifteen, 2))


def _in_container() -> bool:
    """Whether this is running inside a container.

    Worth knowing on the diagnostics page: it changes what the memory figures
    mean and explains why mDNS or WebRTC might misbehave.
    """
    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def collect_host_metrics() -> HostMetrics:
    """Blocking; call through a thread."""
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    try:
        boot = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
    except (OSError, ValueError):
        boot = None

    return HostMetrics(
        cpu_percent=psutil.cpu_percent(interval=CPU_SAMPLE_SECONDS),
        cpu_count=psutil.cpu_count(logical=True) or 1,
        load_average=_load_average(),
        memory_total_bytes=memory.total,
        memory_used_bytes=memory.used,
        memory_percent=memory.percent,
        swap_used_bytes=swap.used,
        boot_time=boot,
    )


def collect_process_metrics() -> ProcessMetrics:
    """Blocking; call through a thread."""
    process = psutil.Process()

    try:
        children = process.children(recursive=True)
    except (psutil.Error, OSError):
        children = []

    ffmpeg = 0
    for child in children:
        try:
            if "ffmpeg" in (child.name() or "").lower():
                ffmpeg += 1
        except (psutil.Error, OSError):
            # The child exited between listing and inspection, which is normal
            # while a recorder is restarting.
            continue

    open_files: int | None
    try:
        open_files = len(process.open_files())
    except (psutil.Error, OSError, NotImplementedError):
        open_files = None

    return ProcessMetrics(
        pid=process.pid,
        # cpu_percent() without an interval reports usage since the previous
        # call, which for a long-lived process is the figure we want.
        cpu_percent=process.cpu_percent(),
        memory_rss_bytes=process.memory_info().rss,
        threads=process.num_threads(),
        open_files=open_files,
        children=len(children),
        ffmpeg_children=ffmpeg,
    )


def collect_disk_metrics(paths: dict[str, Path]) -> list[DiskMetrics]:
    """Blocking; call through a thread."""
    metrics: list[DiskMetrics] = []
    for label, path in paths.items():
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            metrics.append(
                DiskMetrics(
                    path=f"{label}: {path}",
                    total_bytes=0,
                    used_bytes=0,
                    free_bytes=0,
                    available=False,
                    reason=str(exc),
                )
            )
            continue
        metrics.append(
            DiskMetrics(
                path=f"{label}: {path}",
                total_bytes=usage.total,
                used_bytes=usage.used,
                free_bytes=usage.free,
                available=True,
            )
        )
    return metrics


def detect_tooling(settings: Settings) -> dict[str, str | None]:
    """Where the external binaries actually are.

    A missing FFmpeg is the single most common cause of "nothing records", and
    it is far easier to see here than in a stack trace.
    """
    return {
        "ffmpeg": shutil.which(settings.ffmpeg_binary),
        "ffprobe": shutil.which(settings.ffprobe_binary),
        "go2rtc": shutil.which(settings.go2rtc_binary),
    }


async def collect(settings: Settings, *, subsystems: dict[str, Any] | None = None) -> Diagnostics:
    """Gather everything. The blocking parts run off the event loop."""
    disk_paths = {"data": settings.data_dir, "recordings": settings.recordings_dir}

    host, process, disks, tooling = await asyncio.gather(
        asyncio.to_thread(collect_host_metrics),
        asyncio.to_thread(collect_process_metrics),
        asyncio.to_thread(collect_disk_metrics, disk_paths),
        asyncio.to_thread(detect_tooling, settings),
    )

    return Diagnostics(
        version=__version__,
        generated_at=datetime.now(UTC),
        python_version=sys.version.split()[0],
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        container=_in_container(),
        host=host,
        process=process,
        disks=disks,
        tooling=tooling,
        subsystems=subsystems or {},
    )


def to_payload(diagnostics: Diagnostics) -> dict[str, Any]:
    """Serialise for the API and for the downloadable troubleshooting bundle.

    Passed through redaction on the way out. Nothing here is expected to contain
    a secret, but a subsystem could grow a field that does, and a diagnostics
    bundle is exactly the thing a user pastes into a public issue.
    """
    payload: dict[str, Any] = {
        "version": diagnostics.version,
        "generated_at": diagnostics.generated_at.isoformat(),
        "python_version": diagnostics.python_version,
        "platform": diagnostics.platform,
        "container": diagnostics.container,
        "host": {
            "cpu_percent": diagnostics.host.cpu_percent,
            "cpu_count": diagnostics.host.cpu_count,
            "load_average": list(diagnostics.host.load_average)
            if diagnostics.host.load_average
            else None,
            "memory_total_bytes": diagnostics.host.memory_total_bytes,
            "memory_used_bytes": diagnostics.host.memory_used_bytes,
            "memory_percent": diagnostics.host.memory_percent,
            "swap_used_bytes": diagnostics.host.swap_used_bytes,
            "boot_time": diagnostics.host.boot_time.isoformat()
            if diagnostics.host.boot_time
            else None,
        },
        "process": {
            "pid": diagnostics.process.pid,
            "cpu_percent": diagnostics.process.cpu_percent,
            "memory_rss_bytes": diagnostics.process.memory_rss_bytes,
            "threads": diagnostics.process.threads,
            "open_files": diagnostics.process.open_files,
            "children": diagnostics.process.children,
            "ffmpeg_children": diagnostics.process.ffmpeg_children,
        },
        "disks": [
            {
                "path": disk.path,
                "total_bytes": disk.total_bytes,
                "used_bytes": disk.used_bytes,
                "free_bytes": disk.free_bytes,
                "available": disk.available,
                "reason": disk.reason,
            }
            for disk in diagnostics.disks
        ],
        "tooling": diagnostics.tooling,
        "subsystems": diagnostics.subsystems,
    }
    return dict(redact_mapping(payload))
