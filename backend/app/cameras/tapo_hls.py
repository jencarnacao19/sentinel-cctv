"""Short-lived, on-demand HLS for Safari battery-camera playback.

One reserved Direct Stream feeds the existing H264/AAC normaliser and a cheap
copy-only HLS muxer. Temporary fragments expire with the session; they are not
recordings, and neither go2rtc nor a continuous camera worker is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.cameras.tapo_battery import (
    LiveSession,
    TapoBatteryError,
    TapoBatteryUnavailableError,
    get_manager,
)
from app.config import Settings
from app.database.models import Camera
from app.logging_config import get_logger
from app.system import privacy

logger = get_logger("CAMERA")
IDLE_SECONDS = 30.0
START_TIMEOUT_SECONDS = 20.0
MEDIA_NAME = re.compile(r"^(?:playlist\.m3u8|segment\d+\.ts)$")


@dataclass
class HlsSession:
    camera_id: str
    token: str
    owner: str
    live: LiveSession
    directory: Path
    touched: float = field(default_factory=time.monotonic)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    task: asyncio.Task[None] | None = None


class BatteryHlsManager:
    def __init__(self) -> None:
        self.sessions: dict[str, HlsSession] = {}

    async def open(self, camera: Camera, settings: Settings, *, owner: str) -> str:
        manager = get_manager()
        await manager.reserve_live(camera.id)
        live: LiveSession | None = None
        temporary = None
        job: HlsSession | None = None
        try:
            live = await manager.open_live(camera, quality="HD")
            temporary = tempfile.TemporaryDirectory(prefix="sentinel-tapo-live-")
            job = HlsSession(
                camera_id=camera.id,
                token=secrets.token_urlsafe(32),
                owner=owner,
                live=live,
                directory=Path(temporary.name),
            )
            self.sessions[job.token] = job
            job.task = asyncio.create_task(self._run(job, settings), name="tapo-native-hls")
            await asyncio.wait_for(job.ready.wait(), timeout=START_TIMEOUT_SECONDS)
            if job.error or job.task.done():
                raise TapoBatteryUnavailableError(job.error or "The camera stream ended.")

            # The runner owns all cleanup once the response can reach a client.
            # Keep the TemporaryDirectory alive until that runner has finished.
            def cleanup_directory(_task: asyncio.Task[None]) -> None:
                temporary.cleanup()

            job.task.add_done_callback(cleanup_directory)
            return job.token
        except BaseException:
            if job is not None:
                self.sessions.pop(job.token, None)
            if job is not None and job.task is not None:
                job.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await job.task
            if live is not None:
                await live.close()
            await manager.release_live(camera.id)
            if temporary is not None:
                temporary.cleanup()
            raise

    def resolve(self, camera_id: str, token: str) -> HlsSession | None:
        job = self.sessions.get(token)
        if job is None or job.camera_id != camera_id or job.stop.is_set():
            return None
        if time.monotonic() - job.touched > IDLE_SECONDS:
            job.stop.set()
            return None
        job.touched = time.monotonic()
        return job

    async def close(self, camera_id: str, token: str, *, owner: str) -> None:
        job = self.sessions.get(token)
        if job is None or job.camera_id != camera_id or job.owner != owner:
            return
        job.stop.set()
        if job.task is not None:
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await job.task

    async def close_all(self) -> None:
        for job in list(self.sessions.values()):
            await self.close(job.camera_id, job.token, owner=job.owner)

    async def _run(self, job: HlsSession, settings: Settings) -> None:
        process: asyncio.subprocess.Process | None = None
        tasks: list[asyncio.Task] = []
        try:
            process = await asyncio.create_subprocess_exec(
                settings.ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-probesize",
                "1M",
                "-analyzeduration",
                "1000000",
                "-f",
                "mpegts",
                "-i",
                "pipe:0",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "5",
                "-hls_flags",
                "delete_segments+temp_file+independent_segments",
                "-hls_segment_filename",
                str(job.directory / "segment%d.ts"),
                str(job.directory / "playlist.m3u8"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert process.stdin is not None  # noqa: S101

            async def feed() -> None:
                assert process is not None and process.stdin is not None  # noqa: S101
                try:
                    async for chunk in job.live.chunks(
                        max_seconds=settings.tapo_battery_live_max_seconds,
                        ffmpeg_binary=settings.ffmpeg_binary,
                        stop=job.stop,
                    ):
                        process.stdin.write(chunk)
                        await asyncio.wait_for(process.stdin.drain(), timeout=10.0)
                finally:
                    process.stdin.close()

            async def watch() -> None:
                while not job.stop.is_set():
                    if privacy.get_manager().state.enabled:
                        return
                    if time.monotonic() - job.touched > IDLE_SECONDS:
                        return
                    playlist = job.directory / "playlist.m3u8"
                    if playlist.exists() and playlist.stat().st_size > 0:
                        job.ready.set()
                    await asyncio.sleep(0.2)

            tasks = [asyncio.create_task(feed()), asyncio.create_task(watch())]
            tasks.append(asyncio.create_task(process.wait()))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except TapoBatteryError as exc:
            job.error = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - vendor errors must not expose credentials
            logger.warning(
                "Battery HLS preparation failed", extra={"error_type": type(exc).__name__}
            )
            job.error = "Sentinel could not prepare the camera's native video stream."
        finally:
            job.stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            await job.live.close()
            await get_manager().release_live(job.camera_id)
            self.sessions.pop(job.token, None)
            job.ready.set()


battery_hls = BatteryHlsManager()
