"""Reading low-resolution frames for analysis.

FFmpeg is asked for raw 8-bit greyscale at the analysis resolution and frame
rate, which means the decode, the downscale and the colour conversion all happen
in FFmpeg's optimised code rather than in Python. What arrives here is exactly
what the detector wants, with nothing to convert.

The substream is used when the camera has one. Analysing a 2K main stream for
motion would cost more CPU than recording every camera put together, for no
benefit -- movement worth noticing is obvious at 640x360.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import numpy as np

from app.config import Settings
from app.database.models import Camera
from app.motion.detector import ANALYSIS_HEIGHT, ANALYSIS_WIDTH

#: One greyscale frame at the analysis resolution.
FRAME_BYTES = ANALYSIS_WIDTH * ANALYSIS_HEIGHT

INPUT_TIMEOUT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class FrameSource:
    url: str
    using_substream: bool


def resolve_frame_source(camera: Camera, settings: Settings) -> FrameSource | None:
    """Prefer the substream, and prefer go2rtc over the camera itself."""
    from app.cameras.service import resolve_stream_url

    has_sub = bool(camera.sub_stream_url)
    if settings.record_via_go2rtc:
        suffix = "_sub" if has_sub else ""
        return FrameSource(
            url=(
                f"rtsp://{settings.go2rtc_host}:{settings.go2rtc_rtsp_port}/"
                f"{camera.stream_name}{suffix}"
            ),
            using_substream=has_sub,
        )

    direct = resolve_stream_url(camera, sub=has_sub)
    return FrameSource(url=direct, using_substream=has_sub) if direct else None


def build_frame_command(
    url: str, *, fps: float, ffmpeg_binary: str, timeout_seconds: float = INPUT_TIMEOUT_SECONDS
) -> list[str]:
    """FFmpeg arguments producing raw greyscale frames on stdout."""
    command = [ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if url.startswith(("rtsp://", "rtsps://")):
        command += [
            "-rtsp_transport",
            "tcp",
            "-timeout",
            str(int(timeout_seconds * 1_000_000)),
        ]
    command += [
        "-i",
        url,
        # Audio is irrelevant here and decoding it would be pure waste.
        "-an",
        # fps first so the expensive scale runs only on frames we keep.
        "-vf",
        f"fps={fps:g},scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT}",
        "-pix_fmt",
        "gray",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    return command


class FrameReader:
    """Runs FFmpeg and yields frames as NumPy arrays."""

    def __init__(self, command: list[str]) -> None:
        self._command = command
        self._process: asyncio.subprocess.Process | None = None
        self.last_error: str | None = None

    async def __aenter__(self) -> FrameReader:
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def frames(self) -> AsyncIterator[np.ndarray]:
        """Yield frames until the stream ends."""
        process = self._process
        if process is None or process.stdout is None:
            return

        stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            while True:
                # readexactly, not read: a partial frame would be reinterpreted
                # as the start of the next one and every frame after it would be
                # skewed.
                try:
                    payload = await process.stdout.readexactly(FRAME_BYTES)
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    return
                yield np.frombuffer(payload, dtype=np.uint8).reshape(
                    ANALYSIS_HEIGHT, ANALYSIS_WIDTH
                )
        finally:
            stderr_task.cancel()

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self.last_error = text
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - draining must never break the reader
            return
