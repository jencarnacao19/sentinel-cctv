"""Single-frame capture.

Used by the camera page, by the motion-mask editor and, later, by the Home
Assistant camera entity. Phase 3 routes this through go2rtc so a snapshot costs
no extra connection to the camera; until then it opens the stream directly.

Like every other FFmpeg call in Sentinel this is an argument list, never a shell
string, and it is always bounded by a timeout.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.logging_config import get_logger
from app.security.redaction import redact_url

logger = get_logger("CAMERA")

DEFAULT_SNAPSHOT_TIMEOUT = 15.0


class SnapshotError(RuntimeError):
    """Capture failed. The message is safe to show a user."""


def build_snapshot_command(
    url: str, *, timeout_seconds: float, ffmpeg_binary: str, width: int | None = None
) -> list[str]:
    command = [ffmpeg_binary, "-hide_banner", "-loglevel", "error"]
    if url.startswith(("rtsp://", "rtsps://")):
        command += [
            "-rtsp_transport",
            "tcp",
            "-timeout",
            str(int(timeout_seconds * 1_000_000)),
        ]
    command += ["-i", url, "-frames:v", "1", "-an"]
    if width:
        # -2 keeps the height even, which JPEG encoders require.
        command += ["-vf", f"scale={width}:-2"]
    command += ["-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"]
    return command


async def capture_snapshot(
    url: str, *, timeout_seconds: float = DEFAULT_SNAPSHOT_TIMEOUT, width: int | None = None
) -> bytes:
    """Return one JPEG frame from ``url``."""
    settings = get_settings()
    command = build_snapshot_command(
        url,
        timeout_seconds=timeout_seconds,
        ffmpeg_binary=settings.ffmpeg_binary,
        width=width,
    )
    safe_url = redact_url(url)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SnapshotError("FFmpeg was not found on this system.") from exc
    except OSError as exc:
        raise SnapshotError(f"Could not start FFmpeg: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds + 5)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        logger.warning("Snapshot timed out", extra={"url": safe_url})
        raise SnapshotError("Timed out waiting for a frame from the camera.") from exc

    if process.returncode != 0 or not stdout:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        message = redact_url(detail[-1]) if detail else "FFmpeg produced no image."
        logger.info("Snapshot failed", extra={"url": safe_url, "detail": message})
        raise SnapshotError("Could not capture an image from this camera.")

    return stdout
