"""Browser-safe normalisation for proprietary Tapo Direct Streams.

The vendor session returns an MPEG-TS video payload plus separate G.711 audio
bytes.  Forwarding that transport stream verbatim is unreliable: TC82 firmware
may use HEVC, which many MSE clients cannot decode, and the separate audio is
otherwise lost.  This module joins both local inputs and emits H.264/AAC MPEG-TS
for the existing mpegts.js player.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.logging_config import get_logger

logger = get_logger("CAMERA")

TS_PACKET_BYTES = 188
TS_SYNC_BYTE = 0x47
AUDIO_FRAME_BYTES = 160
OUTPUT_CHUNK_BYTES = 64 * 1024
PIPE_DRAIN_TIMEOUT_SECONDS = 15.0
PROBE_RESPONSES = 8


class DirectStreamNormalisationError(RuntimeError):
    """The local media arrived but could not be prepared for a browser."""


def build_normalise_command(
    ffmpeg_binary: str,
    *,
    audio_fd: int | None,
    audio_format: str = "alaw",
    audio_rate: int = 8000,
) -> list[str]:
    """Build the bounded-latency H.264/AAC Direct Stream pipeline."""
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-fflags",
        "+genpts+discardcorrupt",
        "-flags",
        "low_delay",
        "-probesize",
        "1M",
        "-analyzeduration",
        "1000000",
        "-f",
        "mpegts",
        "-i",
        "pipe:0",
    ]
    if audio_fd is not None:
        command.extend(
            [
                "-thread_queue_size",
                "256",
                "-f",
                audio_format,
                "-ar",
                str(audio_rate),
                "-ac",
                "1",
                "-i",
                f"/dev/fd/{audio_fd}",
            ]
        )
    command.extend(
        [
            "-map",
            "0:v:0",
        ]
    )
    if audio_fd is not None:
        command.extend(
            [
                "-map",
                "1:a:0",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-ar",
                "48000",
                "-ac",
                "1",
            ]
        )
    else:
        command.append("-an")
    command.extend(
        [
            # TC82 Direct Stream may be HEVC. Decode locally and cap it at 1080p
            # so iPhone, Home Assistant WebViews and Chromium all receive the same
            # broadly-supported H.264 profile without asking the camera to reopen.
            "-vf",
            r"scale=min(1920\,iw):-2,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "main",
            "-g",
            "30",
            "-keyint_min",
            "30",
            "-sc_threshold",
            "0",
            "-max_muxing_queue_size",
            "1024",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-mpegts_flags",
            "+resend_headers",
            "-flush_packets",
            "1",
            "-f",
            "mpegts",
            "pipe:1",
        ]
    )
    return command


async def _stderr_tail(stream: asyncio.StreamReader | None) -> list[str]:
    if stream is None:
        return []
    tail: list[str] = []
    while line := await stream.readline():
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            tail.append(text)
            del tail[:-8]
    return tail


async def _feed(
    process: asyncio.subprocess.Process,
    responses: AsyncIterator[Any],
    *,
    audio_fd: int | None,
    should_stop: Callable[[], bool],
) -> None:
    if process.stdin is None:
        raise DirectStreamNormalisationError("FFmpeg did not open its video input.")

    video = bytearray()
    audio = bytearray()
    try:
        async for response in responses:
            if should_stop():
                break
            if getattr(response, "mimetype", None) != "video/mp2t":
                continue

            payload = getattr(response, "audioPayload", None)
            if payload and audio_fd is not None:
                audio.extend(payload)
                while len(audio) >= AUDIO_FRAME_BYTES:
                    frame = bytes(audio[:AUDIO_FRAME_BYTES])
                    del audio[:AUDIO_FRAME_BYTES]
                    try:
                        os.write(audio_fd, frame)
                    except BlockingIOError:
                        # The video must stay live even if an overloaded client
                        # temporarily cannot consume a few milliseconds of audio.
                        pass
                    except OSError:
                        break

            video.extend(getattr(response, "plaintext", b""))
            while len(video) >= TS_PACKET_BYTES and video[0] != TS_SYNC_BYTE:
                position = video.find(TS_SYNC_BYTE, 1)
                if position < 0:
                    video.clear()
                    break
                del video[:position]
            complete_video = len(video) - (len(video) % TS_PACKET_BYTES)
            if complete_video:
                process.stdin.write(video[:complete_video])
                del video[:complete_video]
                try:
                    await asyncio.wait_for(
                        process.stdin.drain(), timeout=PIPE_DRAIN_TIMEOUT_SECONDS
                    )
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    break
    finally:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            process.stdin.close()
            await process.stdin.wait_closed()
        if audio_fd is not None:
            with contextlib.suppress(OSError):
                os.close(audio_fd)


async def normalise_direct_stream(
    responses: AsyncIterator[Any],
    *,
    ffmpeg_binary: str,
    should_stop: Callable[[], bool],
) -> AsyncIterator[bytes]:
    """Yield browser-safe MPEG-TS while consuming one local vendor session."""
    buffered: list[Any] = []
    audio_format = "alaw"
    async for response in responses:
        buffered.append(response)
        payload = getattr(response, "audioPayload", None)
        if payload:
            payload_type = getattr(response, "audioPayloadType", None)
            payload_type = getattr(payload_type, "value", payload_type)
            audio_format = "mulaw" if payload_type == 0 else "alaw"
            break
        if len(buffered) >= PROBE_RESPONSES or should_stop():
            break

    if should_stop():
        return
    if not buffered:
        raise DirectStreamNormalisationError(
            "The camera authenticated but ended the stream without sending video."
        )

    async def replay() -> AsyncIterator[Any]:
        for response in buffered:
            yield response
        async for response in responses:
            yield response

    has_audio = any(getattr(response, "audioPayload", None) for response in buffered)
    audio_read: int | None = None
    audio_write: int | None = None
    if has_audio:
        audio_read, audio_write = os.pipe()
        os.set_blocking(audio_write, False)
    command = build_normalise_command(
        ffmpeg_binary,
        audio_fd=audio_read,
        audio_format=audio_format,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(audio_read,) if audio_read is not None else (),
        )
    except (OSError, ValueError) as exc:
        with contextlib.suppress(OSError):
            if audio_read is not None:
                os.close(audio_read)
        with contextlib.suppress(OSError):
            if audio_write is not None:
                os.close(audio_write)
        raise DirectStreamNormalisationError(
            "Sentinel could not start the local video compatibility pipeline."
        ) from exc

    if audio_read is not None:
        os.close(audio_read)
    feed_task = asyncio.create_task(
        _feed(process, replay(), audio_fd=audio_write, should_stop=should_stop),
        name="tapo-direct-stream-input",
    )
    stderr_task = asyncio.create_task(_stderr_tail(process.stderr), name="tapo-ffmpeg-log")
    emitted = 0
    try:
        if process.stdout is None:
            raise DirectStreamNormalisationError("FFmpeg did not open its media output.")
        while chunk := await asyncio.wait_for(
            process.stdout.read(OUTPUT_CHUNK_BYTES), timeout=PIPE_DRAIN_TIMEOUT_SECONDS
        ):
            emitted += len(chunk)
            yield chunk
        await feed_task
        return_code = await process.wait()
        messages = await stderr_task
        if emitted == 0 and not should_stop():
            logger.warning(
                "Tapo Direct Stream normalisation failed",
                extra={"return_code": return_code, "ffmpeg_tail": " | ".join(messages)},
            )
            raise DirectStreamNormalisationError(
                "The camera video format could not be prepared for this browser."
            )
    except TimeoutError as exc:
        raise DirectStreamNormalisationError(
            "The camera stopped delivering playable video. Close other live views and retry."
        ) from exc
    finally:
        if not feed_task.done():
            feed_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await feed_task
        with contextlib.suppress(OSError):
            if audio_write is not None:
                os.close(audio_write)
        if process.returncode is None:
            process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=3.0)
            if process.returncode is None:
                process.kill()
                await process.wait()
        if not stderr_task.done():
            stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stderr_task
