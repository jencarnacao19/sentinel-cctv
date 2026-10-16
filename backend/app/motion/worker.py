"""The per-camera motion worker.

Ties the pieces together: read a frame, detect, aggregate, persist. Runs one
FFmpeg process per camera reading the substream, which at 640x360 and 5 fps is
roughly a millisecond of analysis per frame -- around 1% of one core for nine
cameras.

Reconnection uses the same backoff ladder as the recorders and never gives up,
because a camera that comes back after a night offline must resume detecting by
itself.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.database.models import Camera, MotionEvent
from app.database.session import session_scope
from app.logging_config import get_logger
from app.motion import frames, service, snapshots
from app.motion.aggregator import (
    AggregatorConfig,
    MotionAggregator,
    MotionWindow,
    Transition,
)
from app.motion.detector import MotionDetector, MotionResult, MotionSettings
from app.streaming.client import Go2RtcUnavailableError
from app.streaming.config import stream_names
from app.streaming.manager import get_manager as get_streaming_manager
from app.supervisor import backoff_delay

logger = get_logger("MOTION")

#: A better snapshot is only worth taking during the opening of an event; after
#: that the interesting moment has passed.
SNAPSHOT_WINDOW_SECONDS = 6.0

#: At most one snapshot request per second, however often the peak improves.
SNAPSHOT_MIN_INTERVAL_SECONDS = 1.0


class MotionWorker:
    """Detects motion on one camera until stopped."""

    def __init__(self, camera: Camera, settings: Settings, config: AggregatorConfig) -> None:
        self.camera_id = camera.id
        self.camera_name = camera.name
        self._settings = settings
        self._source = frames.resolve_frame_source(camera, settings)
        self._detector = MotionDetector(MotionSettings.from_sensitivity(camera.motion_sensitivity))
        self._detector.set_zones(service.parse_zones(camera.motion_zones))
        self._aggregator = MotionAggregator(config=config)
        self._stream_name = stream_names(camera)[0]

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._event: MotionEvent | None = None
        self._last_snapshot_at: datetime | None = None

        self.frames_seen = 0
        self.last_frame_at: datetime | None = None
        self.reconnects = 0
        self.last_error: str | None = None

    # --- Lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"motion:{self.camera_name}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close_open_event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # --- Main loop -------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if self._source is None:
                self.last_error = "This camera has no stream Sentinel can analyse."
                return
            try:
                await self._consume_stream()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.warning(
                    "Motion stream failed",
                    extra={"camera": self.camera_name, "detail": str(exc)},
                    exc_info=True,
                )

            # The stream ended: the camera dropped, go2rtc restarted, or FFmpeg
            # exited. Close any open event before waiting, so it does not appear
            # to run for the whole outage.
            await self._close_open_event()
            if self._stop.is_set():
                return

            self.reconnects += 1
            # A reconnected stream may show a completely different scene, so the
            # learned background is discarded rather than carried over.
            self._detector.reset()
            self._aggregator.reset()

            delay = backoff_delay(attempt)
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _consume_stream(self) -> None:
        assert self._source is not None  # noqa: S101 - checked by the caller
        command = frames.build_frame_command(
            self._source.url,
            fps=self._settings.motion_fps,
            ffmpeg_binary=self._settings.ffmpeg_binary,
        )
        logger.info(
            "Motion detection started",
            extra={
                "camera": self.camera_name,
                "substream": self._source.using_substream,
                "fps": self._settings.motion_fps,
            },
        )

        async with frames.FrameReader(command) as reader:
            async for frame in reader.frames():
                if self._stop.is_set():
                    return
                now = datetime.now(UTC)
                self.frames_seen += 1
                self.last_frame_at = now
                # The analysis is CPU-bound; off the loop so nine cameras do not
                # take turns blocking each other and the API.
                result = await asyncio.to_thread(self._detector.process, frame)
                await self._handle(result, now)

            if reader.last_error:
                self.last_error = reader.last_error

    async def _handle(self, result: MotionResult, now: datetime) -> None:
        outcome = self._aggregator.observe(result, now)

        if outcome.transition is Transition.STARTED and outcome.window is not None:
            async with session_scope() as session:
                camera = await session.get(Camera, self.camera_id)
                if camera is None:
                    return
                self._event = await service.start_event(
                    session, camera, outcome.window, self._aggregator.config
                )
            await self._maybe_snapshot(now, force=True)

        elif outcome.transition is Transition.CONTINUING and self._event is not None:
            if outcome.is_peak:
                async with session_scope() as session:
                    event = await session.get(MotionEvent, self._event.id)
                    if event is not None:
                        await service.update_peak(session, event, outcome.score)
                await self._maybe_snapshot(now)

        elif outcome.transition is Transition.ENDED and outcome.window is not None:
            await self._finish(outcome.window)

    async def _finish(self, window: MotionWindow) -> None:
        if self._event is None:
            return
        event_id = self._event.id
        self._event = None
        self._last_snapshot_at = None

        async with session_scope() as session:
            event = await session.get(MotionEvent, event_id)
            if event is not None:
                await service.finish_event(session, event, window, self._aggregator.config)

    async def _close_open_event(self) -> None:
        outcome = self._aggregator.flush(datetime.now(UTC))
        if outcome.transition is Transition.ENDED and outcome.window is not None:
            await self._finish(outcome.window)

    # --- Snapshots -------------------------------------------------------

    async def _maybe_snapshot(self, now: datetime, *, force: bool = False) -> None:
        """Capture a colour snapshot for the current event.

        Only during the opening seconds, and at most once a second: the goal is
        the best frame of the moment something appeared, not a burst of images.
        """
        event = self._event
        if event is None:
            return
        if not force and now - event.started_at > timedelta(seconds=SNAPSHOT_WINDOW_SECONDS):
            return
        if (
            self._last_snapshot_at is not None
            and (now - self._last_snapshot_at).total_seconds() < SNAPSHOT_MIN_INTERVAL_SECONDS
        ):
            return
        self._last_snapshot_at = now

        try:
            image = await get_streaming_manager().client.snapshot(self._stream_name)
        except Go2RtcUnavailableError:
            return

        saved = await snapshots.save_snapshot(
            self._settings, self.camera_id, event.id, event.started_at, image
        )
        if saved is None:
            return

        async with session_scope() as session:
            stored = await session.get(MotionEvent, event.id)
            if stored is not None:
                stored.snapshot_path = saved.snapshot_path
                stored.thumbnail_path = saved.thumbnail_path

    # --- Reporting -------------------------------------------------------

    def status(self) -> dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "running": self.is_running,
            "frames_seen": self.frames_seen,
            "last_frame_at": self.last_frame_at,
            "reconnects": self.reconnects,
            "using_substream": self._source.using_substream if self._source else False,
            "in_event": self._event is not None,
            "last_error": self.last_error,
        }
