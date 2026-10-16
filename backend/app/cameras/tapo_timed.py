"""Bounded, explicitly requested recording from a Tapo battery camera.

A TC82 or H200 camera is not a continuous recorder and Sentinel does not turn it
into one. It sleeps between events, and that sleep is the entire reason its
battery lasts months instead of hours. What this module adds is a deliberate,
time-boxed exception: an administrator asks for *this camera*, for *this many
minutes*, having been shown the current charge and told plainly that the camera
stays awake for the whole period and may end up flat.

Everything about the design follows from that being a bounded exception:

* the session has a deadline from the moment it starts and cannot outlive it;
* only one runs per camera, and it shares the same reservation as live view, so
  a timed recording and a live view can never fight over the radio;
* global privacy mode, the Stop button and server shutdown all end it at once;
* nothing here is ever started automatically -- there is no schedule, no motion
  trigger and no retry-tomorrow. A session exists because a person asked for it.

**Partial results are first-class.** Battery firmware closes the Direct Stream
on its own, batteries go flat, and servers get restarted. In every one of those
cases the footage captured up to that moment is real footage and is kept: media
is written to a transport-stream part file as it arrives, and that file is
remuxed into a playable MP4 and indexed whatever the reason for stopping. The
reported outcome says what actually happened. A session that ran eleven of its
sixty minutes is reported as eleven minutes, never as sixty.

Reconnection is local only. If the stream closes early the camera is re-opened
over the LAN with backoff until the deadline; the TP-Link cloud is never
contacted, on this path or any other.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

from sqlalchemy import select

from app.cameras import tapo_battery
from app.cameras.probe import ProbeError, probe_stream
from app.config import Settings, get_settings
from app.database.models import (
    Camera,
    CameraKind,
    RecordingSegment,
    SegmentOrigin,
    SegmentStatus,
)
from app.database.session import session_scope
from app.logging_config import get_logger
from app.storage.root import get_storage_root

logger = get_logger("CAMERA")

#: Offered in the interface. Anything else has to be typed deliberately.
DURATION_PRESETS_MINUTES: tuple[int, ...] = (5, 10, 15, 30, 60)

#: The absolute ceiling, independent of any administrator setting. Two hours of
#: continuous radio and encoder use is already far beyond what this hardware is
#: built for; allowing more would be pretending otherwise.
MAX_DURATION_MINUTES = 120
MIN_DURATION_MINUTES = 1

#: The administrator-configurable ceiling, stored in the settings table so an
#: installation can be more conservative than the hard limit above.
MAX_MINUTES_SETTING_KEY = "cameras.tapo_timed_max_minutes"
DEFAULT_MAX_MINUTES = 60

#: Backoff for a Direct Stream the firmware closed early. Bounded and local.
RECONNECT_DELAYS_SECONDS: tuple[float, ...] = (2.0, 4.0, 8.0, 15.0, 30.0)

#: How many consecutive failed reopens end the session early. A camera that has
#: refused six times over roughly a minute is not coming back within a window
#: that matters, and continuing would only keep an operator watching a counter.
MAX_CONSECUTIVE_RECONNECT_FAILURES = 6

#: Below this the part file is not worth remuxing: no decoder will produce a
#: frame from it, and indexing it would put an unplayable entry in the timeline.
MIN_USEFUL_PART_BYTES = 64 * 1024

#: Long enough for FFmpeg to remux even a two-hour transport stream (a copy, so
#: it is I/O bound), short enough that shutdown cannot hang on it.
REMUX_TIMEOUT_SECONDS = 120.0

_PART_SUFFIX = ".part.ts"
_PART_PREFIX = "tapo-manual-"


def _file_size(path: Path) -> int:
    """Size in bytes, or zero when the file is not there."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


class TimedRecordingState(StrEnum):
    """Where a session is. Reported verbatim; none of these are decorative."""

    STARTING = "starting"
    RECORDING = "recording"
    RECONNECTING = "reconnecting"
    """The stream closed early and Sentinel is reopening it over the LAN."""
    STOPPING = "stopping"
    COMPLETED = "completed"
    """The full requested duration was captured."""
    PARTIAL = "partial"
    """Less than the requested duration was captured, and it was kept."""
    FAILED = "failed"
    """Nothing playable was captured."""


class StopReason(StrEnum):
    DURATION_REACHED = "duration_reached"
    OPERATOR = "operator"
    PRIVACY = "privacy"
    SHUTDOWN = "shutdown"
    CAMERA_UNREACHABLE = "camera_unreachable"
    ERROR = "error"


class TimedRecordingError(RuntimeError):
    """A safe, user-facing failure. Never carries vendor text."""


class TimedRecordingBusyError(TimedRecordingError):
    pass


class TimedRecordingUnsupportedError(TimedRecordingError):
    pass


_FINAL_STATES = frozenset(
    {TimedRecordingState.COMPLETED, TimedRecordingState.PARTIAL, TimedRecordingState.FAILED}
)

_STOP_MESSAGES: dict[StopReason, str] = {
    StopReason.DURATION_REACHED: "The requested duration was recorded.",
    StopReason.OPERATOR: "Stopped by an administrator.",
    StopReason.PRIVACY: "Stopped because privacy mode was switched on.",
    StopReason.SHUTDOWN: "Stopped because Sentinel shut down.",
    StopReason.CAMERA_UNREACHABLE: "Camera powered off before the requested duration.",
    StopReason.ERROR: "The recording stopped because the camera session failed.",
}


@dataclass(frozen=True, slots=True)
class TimedRecordingStatus:
    """Everything the interface needs, and nothing the camera said verbatim."""

    camera_id: str
    state: TimedRecordingState
    requested_seconds: int
    started_at: datetime
    ends_at: datetime
    seconds_remaining: int
    seconds_recorded: int
    bytes_recorded: int
    connection: Literal["connecting", "connected", "reconnecting", "closed"]
    reconnects: int
    battery_percent_at_start: int | None
    message: str
    stop_reason: StopReason | None = None
    segment_id: str | None = None
    """Set once the clip is in the timeline, whether complete or partial."""

    @property
    def finished(self) -> bool:
        return self.state in _FINAL_STATES


@dataclass(slots=True)
class _Session:
    camera_id: str
    camera_name: str
    requested_seconds: int
    started_at: datetime
    deadline: float
    """Monotonic, so a clock adjustment cannot extend or truncate a session."""
    part_path: Path
    relative_output: str
    battery_percent_at_start: int | None
    state: TimedRecordingState = TimedRecordingState.STARTING
    connection: Literal["connecting", "connected", "reconnecting", "closed"] = "connecting"
    bytes_recorded: int = 0
    reconnects: int = 0
    stop_reason: StopReason | None = None
    segment_id: str | None = None
    message: str = "Opening the camera's Direct Stream."
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    ended_at_monotonic: float | None = None
    """When the capture stopped.

    Set the moment the session ends, and used instead of "now" from then on. A
    finished session keeps being read -- the interface shows its outcome -- and
    without this its reported duration would go on growing toward the requested
    length, so a recording stopped after ten seconds would eventually claim the
    full hour. That is precisely the lie this feature must not tell.
    """
    captured_seconds: float = 0.0
    """Set from the finished file's real duration once it has been probed."""

    def elapsed(self) -> float:
        reference = self.ended_at_monotonic if self.ended_at_monotonic is not None else _monotonic()
        return max(0.0, reference - (self.deadline - self.requested_seconds))


def _monotonic() -> float:
    return time.monotonic()


def _relative_paths(camera_id: str, started_at: datetime) -> tuple[str, str]:
    """Where the clip lives, relative to the storage root.

    Kept out of the continuous ``segment-*`` tree on purpose: the indexer scans
    that tree by filename and would either ignore these files or mistake them
    for segments it owns. A manual clip is inserted into the index directly, by
    the code that produced it and therefore knows its real bounds.
    """
    stamp = int(started_at.timestamp())
    directory = f"{camera_id}/manual/{started_at:%Y}/{started_at:%m}/{started_at:%d}"
    return (
        f"{directory}/{_PART_PREFIX}{stamp}.mp4",
        f"{directory}/{_PART_PREFIX}{stamp}{_PART_SUFFIX}",
    )


def resolve_duration(minutes: int, *, configured_max: int) -> int:
    """Validate a requested duration against both ceilings."""
    ceiling = max(MIN_DURATION_MINUTES, min(configured_max, MAX_DURATION_MINUTES))
    if minutes < MIN_DURATION_MINUTES:
        raise TimedRecordingError(
            f"A timed recording must be at least {MIN_DURATION_MINUTES} minute long."
        )
    if minutes > ceiling:
        raise TimedRecordingError(
            f"This installation allows timed recordings of at most {ceiling} minutes."
        )
    return minutes * 60


async def _remux(part: Path, output: Path, *, ffmpeg_binary: str) -> bool:
    """Turn the captured transport stream into a playable MP4.

    Video is stream-copied. G.711 audio is normalised to AAC because MP4 and
    mobile browsers cannot carry the camera's PCMA/PCMU track directly. FFmpeg
    is deliberately given a possibly truncated input -- transport streams are
    designed to survive that, and salvaging the complete packets is the whole
    reason media is written in that container first.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-fflags",
        "+genpts+discardcorrupt",
        "-i",
        str(part),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=REMUX_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        logger.warning("Timed recording remux timed out", extra={"path": str(output)})
        return False
    if process.returncode != 0 or await asyncio.to_thread(_file_size, output) == 0:
        logger.warning(
            "Timed recording remux failed",
            extra={"path": str(output), "detail": stderr.decode("utf-8", "replace")[:200]},
        )
        return False
    return True


class TimedRecordingManager:
    """Owns every in-flight timed recording, one per battery camera."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()
        self._privacy_paused = False

    # --- privacy contract (see app.system.privacy) -----------------------

    def initialise_privacy(self, paused: bool) -> None:
        self._privacy_paused = paused

    async def set_privacy_paused(self, paused: bool) -> None:
        self._privacy_paused = paused
        if paused:
            await self.stop_all(StopReason.PRIVACY)

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Finalise anything a previous run left behind.

        A crash or a hard power cut leaves a part file with no session to close
        it. That media is still real, so it is remuxed and indexed on the next
        start rather than being left on disk where retention will eventually
        delete a clip nobody knew existed.
        """
        try:
            await self._recover_orphans()
        except Exception:
            logger.exception("Recovering interrupted timed recordings failed")

    async def stop(self) -> None:
        await self.stop_all(StopReason.SHUTDOWN)

    async def stop_all(self, reason: StopReason) -> None:
        """End every session and wait for each to write out its clip."""
        async with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.stop_reason = session.stop_reason or reason
            session.stop.set()
        for session in sessions:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.finished.wait(), timeout=REMUX_TIMEOUT_SECONDS + 30)

    # --- reads -----------------------------------------------------------

    def status(self, camera_id: str) -> TimedRecordingStatus | None:
        session = self._sessions.get(camera_id)
        return None if session is None else self._snapshot(session)

    def active(self, camera_id: str) -> bool:
        session = self._sessions.get(camera_id)
        return session is not None and not session.finished.is_set()

    def any_active(self) -> bool:
        """Whether a battery clip is currently being written to recording storage."""
        return any(not session.finished.is_set() for session in self._sessions.values())

    def _snapshot(self, session: _Session) -> TimedRecordingStatus:
        if session.ended_at_monotonic is not None:
            # Finished. Nothing is left to run, and what was captured is the
            # probed length of the file rather than a share of the clock.
            remaining = 0.0
            recorded = session.captured_seconds or session.elapsed()
        else:
            remaining = max(0.0, session.deadline - time.monotonic())
            recorded = max(0.0, session.requested_seconds - remaining)
        return TimedRecordingStatus(
            camera_id=session.camera_id,
            state=session.state,
            requested_seconds=session.requested_seconds,
            started_at=session.started_at,
            ends_at=session.started_at + timedelta(seconds=session.requested_seconds),
            seconds_remaining=int(remaining),
            seconds_recorded=int(recorded),
            bytes_recorded=session.bytes_recorded,
            connection=session.connection,
            reconnects=session.reconnects,
            battery_percent_at_start=session.battery_percent_at_start,
            message=session.message,
            stop_reason=session.stop_reason,
            segment_id=session.segment_id,
        )

    # --- start / stop ----------------------------------------------------

    async def begin(
        self,
        camera: Camera,
        *,
        duration_seconds: int,
        battery_percent: int | None,
        requested_by: str,
    ) -> TimedRecordingStatus:
        """Reserve the camera, open the stream, and start writing.

        The vendor session is opened here, before this returns, for the same
        reason live view does it: a rejected credential or an unreachable camera
        must be an error on the request that asked for it, not a session that
        appears to be running and produces nothing.
        """
        if camera.kind is not CameraKind.TAPO_BATTERY:
            raise TimedRecordingUnsupportedError("This is not a Tapo battery camera.")
        if self._privacy_paused:
            raise TimedRecordingError("Privacy mode is on, so no camera may be opened.")

        battery_manager = tapo_battery.get_manager()
        async with self._lock:
            existing = self._sessions.get(camera.id)
            if existing is not None and not existing.finished.is_set():
                raise TimedRecordingBusyError(
                    "This camera already has a timed recording in progress."
                )
            # Shared with live view on purpose: one radio, one session.
            try:
                await battery_manager.reserve_live(camera.id)
            except tapo_battery.TapoBatteryBusyError as exc:
                raise TimedRecordingBusyError(
                    "This camera already has a live view or recording open."
                ) from exc

            started_at = datetime.now(UTC)
            try:
                async with session_scope() as db:
                    root = await get_storage_root(db, self._settings)
                relative_output, relative_part = _relative_paths(camera.id, started_at)
                part_path = root / relative_part
                part_path.parent.mkdir(parents=True, exist_ok=True)
                # Prove the camera answers before reporting a running session.
                live = await battery_manager.open_live(camera, quality="HD")
            except Exception:
                await battery_manager.release_live(camera.id)
                raise

            session = _Session(
                camera_id=camera.id,
                camera_name=camera.name,
                requested_seconds=duration_seconds,
                started_at=started_at,
                deadline=time.monotonic() + duration_seconds,
                part_path=part_path,
                relative_output=relative_output,
                battery_percent_at_start=battery_percent,
            )
            session.state = TimedRecordingState.RECORDING
            session.connection = "connected"
            session.message = "Recording from the camera's Direct Stream."
            session.task = asyncio.create_task(
                self._run(camera, session, live),
                name=f"tapo-timed:{camera.id}",
            )
            self._sessions[camera.id] = session

        logger.warning(
            "Timed Tapo battery recording started by an administrator",
            extra={
                "camera": camera.name,
                "camera_id": camera.id,
                "minutes": duration_seconds // 60,
                "battery_percent": battery_percent,
                "requested_by": requested_by,
            },
        )
        return self._snapshot(session)

    async def request_stop(self, camera_id: str, reason: StopReason) -> TimedRecordingStatus:
        session = self._sessions.get(camera_id)
        if session is None or session.finished.is_set():
            raise TimedRecordingError("No timed recording is running for this camera.")
        session.stop_reason = session.stop_reason or reason
        session.state = TimedRecordingState.STOPPING
        session.message = "Closing the camera session and saving the clip."
        session.stop.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session.finished.wait(), timeout=REMUX_TIMEOUT_SECONDS + 30)
        return self._snapshot(session)

    # --- the session itself ----------------------------------------------

    async def _run(self, camera: Camera, session: _Session, live: tapo_battery.LiveSession) -> None:
        battery_manager = tapo_battery.get_manager()
        consecutive_failures = 0
        try:
            while True:
                await self._drain(session, live)
                if session.stop.is_set() or time.monotonic() >= session.deadline:
                    session.stop_reason = session.stop_reason or StopReason.DURATION_REACHED
                    break

                # The firmware closed the stream before the deadline. Reopen it
                # locally, with backoff, until the timer runs out.
                session.state = TimedRecordingState.RECONNECTING
                session.connection = "reconnecting"
                session.message = "The camera closed the stream; reopening it on the local network."
                while True:
                    delay = RECONNECT_DELAYS_SECONDS[
                        min(consecutive_failures, len(RECONNECT_DELAYS_SECONDS) - 1)
                    ]
                    remaining = session.deadline - time.monotonic()
                    if remaining <= delay:
                        session.stop_reason = StopReason.DURATION_REACHED
                        return
                    try:
                        await asyncio.wait_for(session.stop.wait(), timeout=delay)
                        session.stop_reason = session.stop_reason or StopReason.OPERATOR
                        return
                    except TimeoutError:
                        pass

                    try:
                        live = await battery_manager.open_live(camera, quality="HD")
                    except tapo_battery.TapoBatteryError:
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_CONSECUTIVE_RECONNECT_FAILURES:
                            session.stop_reason = StopReason.CAMERA_UNREACHABLE
                            return
                        # Retry opening the camera after the next bounded delay.
                        # The previous LiveSession is already closed and must
                        # never be drained again.
                        continue
                    consecutive_failures = 0
                    session.reconnects += 1
                    session.state = TimedRecordingState.RECORDING
                    session.connection = "connected"
                    session.message = "Recording from the camera's Direct Stream."
                    break
        except asyncio.CancelledError:
            session.stop_reason = session.stop_reason or StopReason.SHUTDOWN
            raise
        except Exception:
            logger.exception(
                "Timed Tapo battery recording failed",
                extra={"camera": session.camera_name, "camera_id": session.camera_id},
            )
            session.stop_reason = session.stop_reason or StopReason.ERROR
        finally:
            # Stamped before anything that can take time, so the reported
            # duration is when the camera stopped sending rather than when the
            # remux happened to finish.
            session.ended_at_monotonic = time.monotonic()
            session.connection = "closed"
            session.state = TimedRecordingState.STOPPING
            with contextlib.suppress(Exception):
                await battery_manager.abort_live(session.camera_id)
            try:
                await self._finalise(session)
            except Exception:
                # Swallowing this silently used to leave the session in
                # STOPPING, which is not a terminal state -- so the interface
                # polled a session that had already ended and showed "Stopping"
                # for ever. Whatever went wrong, the session is over and has to
                # say so.
                logger.exception(
                    "Saving a timed Tapo battery recording failed",
                    extra={"camera": session.camera_name, "camera_id": session.camera_id},
                )
                session.state = TimedRecordingState.FAILED
                session.message = (
                    "The recording ended but Sentinel could not save it. The captured data was "
                    "kept on disk and will be recovered the next time Sentinel starts."
                )
            session.finished.set()

    async def _drain(self, session: _Session, live: tapo_battery.LiveSession) -> None:
        """Append everything this stream gives us to the part file."""
        remaining = session.deadline - time.monotonic()
        if remaining <= 0:
            await live.close()
            return
        handle = session.part_path.open("ab")
        try:
            async for chunk in live.chunks(
                max_seconds=remaining,
                ffmpeg_binary=self._settings.ffmpeg_binary,
                stop=session.stop,
            ):
                # Written straight through: a session that is killed between
                # ticks must still leave a file with the frames it had.
                await asyncio.to_thread(handle.write, chunk)
                session.bytes_recorded += len(chunk)
        except tapo_battery.TapoBatteryError as exc:
            session.message = str(exc)
        finally:
            await asyncio.to_thread(handle.flush)
            handle.close()

    async def _finalise(self, session: _Session) -> None:
        """Remux whatever was captured and put it in the timeline."""
        reason = session.stop_reason or StopReason.ERROR
        part = session.part_path
        size = await asyncio.to_thread(_file_size, part)
        if size < MIN_USEFUL_PART_BYTES:
            await asyncio.to_thread(_discard, part)
            session.state = TimedRecordingState.FAILED
            session.message = (
                "No playable video arrived from the camera. "
                f"{_STOP_MESSAGES.get(reason, '')}".strip()
            )
            logger.warning(
                "Timed Tapo battery recording produced no usable media",
                extra={"camera": session.camera_name, "camera_id": session.camera_id},
            )
            return

        async with session_scope() as db:
            root = await get_storage_root(db, self._settings)
        output = root / session.relative_output
        if not await _remux(part, output, ffmpeg_binary=self._settings.ffmpeg_binary):
            session.state = TimedRecordingState.FAILED
            session.message = (
                "The captured video could not be prepared for playback. It was kept and will be "
                "retried the next time Sentinel starts."
            )
            # Deliberately kept: a transient FFmpeg failure should not destroy
            # the only copy of the footage, and start-up recovery retries it.
            # Logged at error level with its size because nothing else will
            # account for it -- retention sums indexed segments, not files.
            logger.error(
                "A timed Tapo battery recording could not be prepared for playback",
                extra={
                    "camera": session.camera_name,
                    "camera_id": session.camera_id,
                    "bytes": size,
                },
            )
            return

        try:
            probe = await probe_stream(str(output), timeout_seconds=20)
            duration = probe.duration_seconds or 0.0
            codec, width, height, fps = probe.codec, probe.width, probe.height, probe.fps
            status = SegmentStatus.COMPLETE
        except ProbeError:
            duration, codec, width, height, fps = 0.0, None, None, None, None
            status = SegmentStatus.DAMAGED

        session.captured_seconds = duration
        complete = reason is StopReason.DURATION_REACHED and duration >= (
            session.requested_seconds * 0.95
        )
        session.state = TimedRecordingState.COMPLETED if complete else TimedRecordingState.PARTIAL
        session.message = self._outcome_message(session, reason, duration, complete)

        async with session_scope() as db:
            segment = RecordingSegment(
                camera_id=session.camera_id,
                path=session.relative_output,
                origin=SegmentOrigin.TAPO_MANUAL,
                external_id=f"manual:{int(session.started_at.timestamp())}",
                started_at=session.started_at,
                ended_at=session.started_at + timedelta(seconds=duration),
                duration_seconds=duration,
                size_bytes=await asyncio.to_thread(_file_size, output),
                codec=codec,
                width=width,
                height=height,
                fps=fps,
                has_motion=False,
                motion_seconds=0.0,
                protected=False,
                status=status,
            )
            db.add(segment)
            await db.flush()
            session.segment_id = segment.id

        # Only now: the clip is indexed, so the recovery marker has done its
        # job. Until this point a crash leaves the part file behind and the
        # next start-up finishes the work.
        await asyncio.to_thread(_discard, part)

        logger.info(
            "Timed Tapo battery recording saved",
            extra={
                "camera": session.camera_name,
                "camera_id": session.camera_id,
                "seconds": round(duration, 1),
                "requested_seconds": session.requested_seconds,
                "reason": reason.value,
                "complete": complete,
            },
        )

    @staticmethod
    def _outcome_message(
        session: _Session, reason: StopReason, duration: float, complete: bool
    ) -> str:
        minutes = duration / 60
        captured = f"{minutes:.1f} minute" + ("" if 0.95 <= minutes <= 1.05 else "s")
        if complete:
            return f"Recorded the requested {session.requested_seconds // 60} minutes."
        return (
            f"{_STOP_MESSAGES.get(reason, 'The recording ended early.')} "
            f"{captured} of the requested {session.requested_seconds // 60} "
            "were saved and added to the timeline."
        )

    # --- crash recovery ---------------------------------------------------

    async def _recover_orphans(self) -> None:
        async with session_scope() as db:
            root = await get_storage_root(db, self._settings)
        if not root.exists():
            return
        parts = await asyncio.to_thread(
            lambda: sorted(root.glob(f"*/manual/*/*/*/{_PART_PREFIX}*{_PART_SUFFIX}"))
        )
        for part in parts:
            with contextlib.suppress(Exception):
                await self._recover_one(root, part)

    async def _recover_one(self, root: Path, part: Path) -> None:
        relative_part = part.relative_to(root).as_posix()
        relative_output = relative_part[: -len(_PART_SUFFIX)] + ".mp4"
        camera_id = relative_part.split("/", 1)[0]
        stamp = part.name[len(_PART_PREFIX) : -len(_PART_SUFFIX)]
        try:
            started_at = datetime.fromtimestamp(int(stamp), tz=UTC)
        except ValueError:
            logger.warning("Unrecognised interrupted recording file", extra={"path": part.name})
            return
        if await asyncio.to_thread(_file_size, part) < MIN_USEFUL_PART_BYTES:
            await asyncio.to_thread(_discard, part)
            return

        external_id = f"manual:{int(started_at.timestamp())}"
        async with session_scope() as db:
            camera = await db.get(Camera, camera_id)
            if camera is None:
                # The camera was removed while this clip was orphaned; its
                # segments went with it, so the file has nothing to belong to.
                await asyncio.to_thread(_discard, part)
                await asyncio.to_thread(_discard, root / relative_output)
                logger.info(
                    "Discarded an orphaned timed recording for a removed camera",
                    extra={"camera_id": camera_id},
                )
                return
            already = await db.scalar(
                select(RecordingSegment.id).where(
                    RecordingSegment.camera_id == camera_id,
                    RecordingSegment.external_id == external_id,
                )
            )
        if already is not None:
            # A previous run indexed it. Checked before remuxing, so a retry can
            # never overwrite the file a timeline entry already points at.
            await asyncio.to_thread(_discard, part)
            return

        output = root / relative_output
        if not await _remux(part, output, ffmpeg_binary=self._settings.ffmpeg_binary):
            # Second independent attempt, second failure: this transport stream
            # is not remuxable, so keeping it only leaks disk that nothing
            # accounts for. Said plainly rather than deleted quietly.
            await asyncio.to_thread(_discard, part)
            logger.error(
                "Discarded an unreadable interrupted timed recording after a second attempt",
                extra={"camera_id": camera_id, "path": relative_part},
            )
            return
        try:
            probe = await probe_stream(str(output), timeout_seconds=20)
            duration = probe.duration_seconds or 0.0
            codec, width, height, fps = probe.codec, probe.width, probe.height, probe.fps
            status = SegmentStatus.COMPLETE
        except ProbeError:
            duration, codec, width, height, fps = 0.0, None, None, None, None
            status = SegmentStatus.DAMAGED

        output_size = await asyncio.to_thread(_file_size, output)
        async with session_scope() as db:
            db.add(
                RecordingSegment(
                    camera_id=camera_id,
                    path=relative_output,
                    origin=SegmentOrigin.TAPO_MANUAL,
                    external_id=external_id,
                    started_at=started_at,
                    ended_at=started_at + timedelta(seconds=duration),
                    duration_seconds=duration,
                    size_bytes=output_size,
                    codec=codec,
                    width=width,
                    height=height,
                    fps=fps,
                    has_motion=False,
                    motion_seconds=0.0,
                    protected=False,
                    status=status,
                )
            )
        await asyncio.to_thread(_discard, part)
        logger.info(
            "Recovered an interrupted timed Tapo recording",
            extra={"camera_id": camera_id, "seconds": round(duration, 1)},
        )


_manager: TimedRecordingManager | None = None


def get_manager() -> TimedRecordingManager:
    global _manager
    if _manager is None:
        _manager = TimedRecordingManager()
    return _manager


def set_manager(manager: TimedRecordingManager | None) -> None:
    """Test helper."""
    global _manager
    _manager = manager
