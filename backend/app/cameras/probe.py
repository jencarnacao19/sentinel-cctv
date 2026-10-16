"""Stream inspection with FFprobe.

Used to answer the one question that matters before a camera is saved: *can
Sentinel actually open this stream, and what is in it?* The codec decides whether
recording can use ``-c copy``; the resolution and frame rate decide whether a
substream is needed for motion detection.

Every invocation is bounded by a timeout and built as an argument list. The URL
carries the camera password, so it is passed as one argv element and never
interpolated into a shell string -- there is no shell in this path at all.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.logging_config import get_logger
from app.security.redaction import redact_url

logger = get_logger("CAMERA")

#: Codecs Sentinel can record with a plain stream copy.
COPYABLE_VIDEO_CODECS = frozenset({"h264", "hevc", "h265", "mjpeg"})


class ProbeError(RuntimeError):
    """FFprobe could not open the stream. The message is safe to show a user."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What FFprobe found in a stream."""

    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_codec: str | None = None
    bitrate_kbps: int | None = None
    duration_seconds: float | None = None
    raw_streams: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_video(self) -> bool:
        return self.codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def can_stream_copy(self) -> bool:
        """Whether recording can avoid re-encoding, which is the difference
        between a few percent of a core and a whole one."""
        return (self.codec or "").lower() in COPYABLE_VIDEO_CODECS

    @property
    def resolution(self) -> str | None:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None


def _parse_frame_rate(value: str | None) -> float | None:
    """FFprobe reports frame rates as the fraction ``25/1`` (or ``0/0``)."""
    if not value:
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denom = float(denominator)
            if denom == 0:
                return None
            return round(float(numerator) / denom, 3)
        return round(float(value), 3)
    except (ValueError, ZeroDivisionError):
        return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        if value in (None, "", "N/A"):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def parse_probe_output(payload: dict[str, Any]) -> ProbeResult:
    """Turn FFprobe's JSON into a :class:`ProbeResult`."""
    streams: list[dict[str, Any]] = payload.get("streams") or []
    fmt: dict[str, Any] = payload.get("format") or {}

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    bitrate_bps = _first_int(
        fmt.get("bit_rate"),
        (video or {}).get("bit_rate"),
    )

    duration = None
    raw_duration = fmt.get("duration")
    if raw_duration not in (None, "", "N/A"):
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None

    return ProbeResult(
        codec=(video or {}).get("codec_name"),
        width=_first_int((video or {}).get("width")),
        height=_first_int((video or {}).get("height")),
        # avg_frame_rate is 0/0 on a live stream that has not been sampled long
        # enough, in which case the negotiated r_frame_rate is the better answer.
        fps=_parse_frame_rate((video or {}).get("avg_frame_rate"))
        or _parse_frame_rate((video or {}).get("r_frame_rate")),
        audio_codec=(audio or {}).get("codec_name"),
        bitrate_kbps=round(bitrate_bps / 1000) if bitrate_bps else None,
        duration_seconds=duration,
        raw_streams=streams,
    )


def build_probe_command(
    url: str, *, timeout_seconds: float, ffprobe_binary: str, transport: str = "tcp"
) -> list[str]:
    """Build the FFprobe argument list.

    TCP transport by default: UDP silently loses packets on a busy network and
    makes a working camera look broken.
    """
    command = [ffprobe_binary, "-hide_banner", "-loglevel", "error"]
    if url.startswith(("rtsp://", "rtsps://")):
        command += [
            "-rtsp_transport",
            transport,
            # Socket I/O timeout in microseconds; without it FFprobe waits
            # indefinitely on a host that accepts the connection then stalls.
            "-timeout",
            str(int(timeout_seconds * 1_000_000)),
        ]
    command += [
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        # One read attempt with a bounded analysis window keeps a slow camera
        # from holding the request open for a minute.
        "-analyzeduration",
        "3000000",
        "-probesize",
        "2000000",
        "-i",
        url,
    ]
    return command


async def probe_stream(
    url: str,
    *,
    timeout_seconds: float | None = None,
    transport: str = "tcp",
) -> ProbeResult:
    """Open ``url`` with FFprobe and describe what is there.

    ``url`` may contain credentials; nothing derived from it is logged unredacted.
    """
    settings = get_settings()
    timeout = timeout_seconds if timeout_seconds is not None else settings.ffprobe_timeout_seconds
    command = build_probe_command(
        url,
        timeout_seconds=timeout,
        ffprobe_binary=settings.ffprobe_binary,
        transport=transport,
    )
    safe_url = redact_url(url)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ProbeError(
            "FFprobe was not found. Sentinel cannot inspect streams without FFmpeg installed."
        ) from exc
    except OSError as exc:
        raise ProbeError(f"Could not start FFprobe: {exc}") from exc

    try:
        # The wall-clock ceiling is slightly above FFprobe's own socket timeout so
        # a well-behaved FFprobe reports the real error before we kill it.
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout + 5)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        logger.warning("Stream probe timed out", extra={"url": safe_url})
        raise ProbeError(
            f"Timed out after {timeout:.0f}s waiting for the stream. "
            "Check the address, the port and the credentials."
        ) from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        message = redact_url(detail[-1]) if detail else "FFprobe reported an error."
        logger.info(
            "Stream probe failed",
            extra={"url": safe_url, "returncode": process.returncode, "detail": message},
        )
        raise ProbeError(_friendly_error(message))

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError("FFprobe returned output Sentinel could not understand.") from exc

    result = parse_probe_output(payload)
    if not result.has_video:
        raise ProbeError("The stream opened but contains no video track.")

    logger.info(
        "Stream probe succeeded",
        extra={
            "url": safe_url,
            "codec": result.codec,
            "resolution": result.resolution,
            "fps": result.fps,
        },
    )
    return result


def _friendly_error(message: str) -> str:
    """Translate the handful of FFprobe errors users actually hit."""
    lowered = message.lower()
    if "401" in lowered or "unauthorized" in lowered:
        return "The camera rejected the username or password."
    if "404" in lowered or "not found" in lowered:
        return "The camera answered, but that stream path does not exist."
    if "connection refused" in lowered:
        return "The camera refused the connection. Check the port."
    if "no route to host" in lowered or "network is unreachable" in lowered:
        return "That address is not reachable from the Sentinel server."
    if "timed out" in lowered or "timeout" in lowered:
        return "The camera did not respond in time."
    if "immediate exit requested" in lowered:
        return "The connection was interrupted before the stream could be read."
    return f"Could not open the stream: {message}"
