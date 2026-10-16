"""Ownership of every recorder.

One :class:`SupervisedProcess` per recording camera, plus background
tickers:

* **Directory pre-creation.** FFmpeg's segment muxer expands ``strftime`` but
  will not create directories, so the next hours must exist before the clock
  rolls into them. A recorder running for weeks never restarts, so this cannot
  be done at start-up only.
* **Indexing.** Picks up completed segments and, on the first pass after a
  restart, whatever the previous run left behind.
* **Storage watch.** If the recordings mount disappears, recording stops rather
  than quietly filling the system disk.
* **Write watch.** An FFmpeg process can stay alive while malformed H.264 setup
  data prevents it from creating a segment. A bounded liveness check restarts
  that input instead of reporting a false healthy recorder forever.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.cameras.service import list_cameras
from app.config import Settings, get_settings
from app.database.models import (
    Camera,
    CameraKind,
    RecorderSource,
    RecordingMode,
    RecordingProfile,
    RecordingSourceMode,
)
from app.database.session import session_scope
from app.events.bus import RECORDING_STARTED, RECORDING_STOPPED, STORAGE_CRITICAL, publish
from app.logging_config import get_logger
from app.recording import corruption, indexer, paths, profiles, recorder
from app.recording.source_policy import (
    CameraSourceState,
    SourceOutcome,
    SourcePolicy,
    SourceReason,
    decode_states,
    encode_states,
    prune_states,
)
from app.storage import retention
from app.storage.root import StorageStatus, check_storage, get_storage_root, prepare_storage
from app.supervisor import SupervisedProcess
from app.system import health, settings_store

if TYPE_CHECKING:  # pragma: no cover - annotation only; schemas imports service
    from app.cameras.schemas import RecordingRuntimeState

logger = get_logger("RECORDING")

DIRECTORY_TICK_SECONDS = 300.0
STORAGE_TICK_SECONDS = 30.0
RECORDER_WATCH_TICK_SECONDS = 30.0

#: A normal segment is one minute. Give a recorder at least two full segment
#: windows to create or update a file before treating a still-running FFmpeg as
#: stalled. The floor also keeps unusually short test/development segments from
#: causing aggressive reconnects.
RECORDER_STALL_MIN_SECONDS = 120.0

#: Retention runs every ten minutes. More often wastes I/O; less often lets the
#: disk get closer to full than the minimum-free-space setting intends.
RETENTION_TICK_SECONDS = 600.0

#: Modes that mean "keep a continuous recording running".
#:
#: Motion-only and scheduled recording are separate features; until they exist,
#: selecting them records nothing rather than pretending otherwise.
CONTINUOUS_MODES = frozenset({RecordingMode.CONTINUOUS})

#: How a recorder process is built. Production always uses
#: :func:`_supervised_recorder`; the seam exists so the test suite can drive the
#: manager's real start/stop path without spawning FFmpeg.
RecorderFactory = Callable[
    [
        str,
        Callable[[], list[str]],
        dict[str, str],
        Callable[[int | None], Awaitable[None]],
        Callable[[str], bool],
    ],
    SupervisedProcess,
]


def _supervised_recorder(
    name: str,
    build_command: Callable[[], list[str]],
    env: dict[str, str],
    on_exit: Callable[[int | None], Awaitable[None]],
    on_output: Callable[[str], bool],
) -> SupervisedProcess:
    return SupervisedProcess(
        name,
        build_command,
        category="RECORDING",
        env=env,
        on_exit=on_exit,
        on_output=on_output,
    )


class RecordingManager:
    """Starts, stops and supervises the FFmpeg recorders."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._recorders: dict[str, SupervisedProcess] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        # Indexing and retention both reconcile the same segment rows and
        # files. Running them together lets retention delete rows while the
        # indexer still holds ORM instances for them, producing StaleDataError
        # at commit. Keep camera/process synchronisation independent, but make
        # the two storage-maintenance passes mutually exclusive.
        self._maintenance_lock = asyncio.Lock()
        self._root: Path | None = None
        self._storage: StorageStatus | None = None
        self._segment_seconds = self._settings.segment_seconds
        #: Per-camera learned source qualification. Replaces the previous
        #: "prefer direct" identifier set, which recorded the *outcome* of blind
        #: alternation rather than any measurement.
        self._source_states: dict[str, CameraSourceState] = {}
        #: The concrete source and profile each running recorder was started
        #: with, so reporting can answer "which path, and why" without
        #: rebuilding an FFmpeg argument list that contains the password.
        self._active_source: dict[str, RecorderSource] = {}
        self._active_profile: dict[str, RecordingProfile] = {}
        self._corruption = corruption.CorruptionRegistry()
        self._suspended_reason: str | None = None
        self._privacy_paused = False
        # A destructive storage operation may briefly stop the recorders while
        # leaving live view, motion controls and the rest of Sentinel alone.
        # This is deliberately separate from privacy mode and from a failed
        # storage mount: neither of those states should be toggled just to make
        # a consistent "delete all footage" boundary.
        self._cleanup_paused = False
        #: One lock per camera, so a per-camera start/stop cannot interleave
        #: with another request for the same camera. The manager-wide lock is
        #: still taken inside it; this one exists to make the read-decide-act
        #: sequence atomic, which the manager lock alone does not.
        self._camera_locks: dict[str, asyncio.Lock] = {}
        #: Test seam. Left as ``None`` in production, where the recorder is
        #: always a real supervised FFmpeg process. Setting it lets the suite
        #: exercise the genuine start/stop bookkeeping -- the lock discipline,
        #: the health counters, the events -- against a process double instead
        #: of mocking the manager itself away.
        self._recorder_factory: RecorderFactory | None = None

    # --- Lifecycle -------------------------------------------------------

    async def start(self) -> None:
        async with session_scope() as session:
            self._root = await get_storage_root(session, self._settings)
            self._segment_seconds = int(
                await settings_store.get_setting(session, settings_store.SEGMENT_SECONDS, 60)
            )
            self._source_states = decode_states(
                await settings_store.get_setting(session, settings_store.RECORDER_SOURCE_POLICY, {})
            )
            legacy = await settings_store.get_setting(
                session, settings_store.RECORDER_DIRECT_CAMERA_IDS, []
            )
            if isinstance(legacy, list) and legacy and not self._source_states:
                # Deliberately discarded rather than imported. That list was
                # produced by the previous blind alternation, so it records
                # which cameras happened to fail last -- not which source is
                # actually better. Importing it would re-pin those cameras to a
                # second RTSP session per camera, which is the condition this
                # policy exists to end. Every camera requalifies from the relay.
                logger.info(
                    "Discarding the legacy learned direct-source list; "
                    "every camera requalifies from the shared relay",
                    extra={"cameras": len(legacy)},
                )

        self._storage = prepare_storage(self._root)
        if not self._storage.available:
            logger.error(
                "Storage is not usable; recording will not start",
                extra={"path": str(self._root), "detail": self._storage.reason},
            )
            self._suspended_reason = self._storage.reason

        if self._settings.testing:
            # Tests drive sync() and the indexer directly; spawning FFmpeg and
            # three tickers per test would make the suite slow and flaky.
            return

        await self.sync()
        self._tasks = [
            asyncio.create_task(self._directory_loop(), name="recording-directories"),
            asyncio.create_task(self._index_loop(), name="recording-index"),
            asyncio.create_task(self._recorder_watch_loop(), name="recording-write-watch"),
            asyncio.create_task(self._storage_loop(), name="recording-storage"),
            asyncio.create_task(self._retention_loop(), name="recording-retention"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        async with self._lock:
            # Stopped concurrently: with a dozen cameras, a serial ten-second
            # SIGTERM grace each would make shutdown take two minutes.
            await asyncio.gather(
                *(process.stop() for process in self._recorders.values()),
                return_exceptions=True,
            )
            self._recorders.clear()
            self._forget_runtime()

    @property
    def privacy_paused(self) -> bool:
        return self._privacy_paused

    def initialise_privacy(self, paused: bool) -> None:
        self._privacy_paused = paused

    async def set_privacy_paused(self, paused: bool) -> None:
        """Stop every recorder without stopping storage/index maintenance."""
        if paused == self._privacy_paused and (not paused or not self._recorders):
            return
        self._privacy_paused = paused
        if paused:
            async with self._lock:
                recorders = list(self._recorders.items())
                results = await asyncio.gather(
                    *(process.stop() for _, process in recorders),
                    return_exceptions=True,
                )
                self._recorders = {
                    camera_id: process
                    for (camera_id, process), result in zip(recorders, results, strict=True)
                    if isinstance(result, BaseException) and process.is_running
                }
                running = set(self._recorders)
                self._active_source = {
                    key: value for key, value in self._active_source.items() if key in running
                }
                self._active_profile = {
                    key: value for key, value in self._active_profile.items() if key in running
                }
                failures = [result for result in results if isinstance(result, BaseException)]
                if failures:
                    raise RuntimeError(
                        f"{len(failures)} recorder(s) did not stop for privacy mode."
                    )
            logger.info("All recording paused by privacy mode")
        else:
            logger.info("Privacy mode ended; resuming configured recorders")
            await self.sync()

    async def set_cleanup_paused(self, paused: bool) -> None:
        """Pause only continuous recorders for an administrator cleanup.

        The all-history cleanup needs the final segment to close before it can
        remove every indexed file. Live view and camera control remain
        available, unlike global privacy mode.
        """
        if paused == self._cleanup_paused and (not paused or not self._recorders):
            return
        self._cleanup_paused = paused
        if paused:
            failures: list[BaseException] = []
            async with self._lock:
                recorders = list(self._recorders.items())
                results = await asyncio.gather(
                    *(process.stop() for _, process in recorders),
                    return_exceptions=True,
                )
                self._recorders = {
                    camera_id: process
                    for (camera_id, process), result in zip(recorders, results, strict=True)
                    if isinstance(result, BaseException) and process.is_running
                }
                running = set(self._recorders)
                self._active_source = {
                    key: value for key, value in self._active_source.items() if key in running
                }
                self._active_profile = {
                    key: value for key, value in self._active_profile.items() if key in running
                }
                failures = [result for result in results if isinstance(result, BaseException)]
            if failures:
                # Deletion never begins with an uncertain writer. Return to the
                # pre-cleanup operating state before surfacing the failure.
                self._cleanup_paused = False
                await self.sync()
                raise RuntimeError(f"{len(failures)} recorder(s) did not stop for storage cleanup.")
            logger.info("Continuous recording paused for storage cleanup")
        else:
            logger.info("Storage cleanup ended; resuming configured recorders")
            await self.sync()

    @contextlib.asynccontextmanager
    async def storage_maintenance(self, *, pause_recorders: bool = False) -> AsyncIterator[None]:
        """Serialise an explicit cleanup with indexing and retention.

        A full cleanup first stops recorders, then lets the normal indexer pick
        up the segments FFmpeg just finalised. The maintenance lock prevents a
        background index/retention pass from racing the deleting transaction.
        """
        if pause_recorders:
            await self.set_cleanup_paused(True)
        try:
            if pause_recorders:
                await self.index_once()
            async with self._maintenance_lock:
                yield
        finally:
            if pause_recorders:
                await self.set_cleanup_paused(False)

    # --- Recorder set ----------------------------------------------------

    async def sync(self) -> None:
        """Bring the set of running recorders in line with the camera list."""
        if self._settings.testing:
            # Reached from the camera endpoints as well as from start(), so the
            # guard has to be here too or every camera created in a test would
            # spawn a real FFmpeg process.
            return

        async with session_scope() as session:
            cameras = await list_cameras(session)

        wanted = {camera.id: camera for camera in cameras if self._should_record(camera)}

        async with self._lock:
            for camera_id in list(self._recorders):
                if camera_id not in wanted:
                    await self._stop_recorder(camera_id)

            for camera_id, camera in wanted.items():
                if camera_id not in self._recorders:
                    await self._start_recorder(camera)

    async def restart_camera(self, camera_id: str) -> None:
        """Rebuild one recorder after that camera's stream source changed."""
        if self._settings.testing:
            return
        async with self._lock:
            await self._stop_recorder(camera_id)
        await self.sync()

    def _should_record(self, camera: Camera) -> bool:
        if self._privacy_paused or self._cleanup_paused or self._suspended_reason is not None:
            return False
        if camera.kind is not CameraKind.RTSP:
            return False
        if not camera.enabled or not camera.recording_enabled:
            return False
        if camera.recording_mode not in CONTINUOUS_MODES:
            return False
        return bool(camera.main_stream_url)

    def camera_recording_state(self, camera: Camera) -> RecordingRuntimeState:
        """The honest state of one camera's recorder.

        ``failed`` is reserved for a camera that is configured to record, is not
        blocked by privacy or storage, and still has no live recorder. It is the
        one answer a toggle must never hide behind an optimistic "on".
        """
        if camera.kind is not CameraKind.RTSP:
            return "unsupported"
        if self._recorders.get(camera.id) is not None and self._recorders[camera.id].is_running:
            return "recording"
        if not camera.enabled or not camera.recording_enabled:
            return "off"
        if camera.recording_mode not in CONTINUOUS_MODES:
            return "off"
        if self._privacy_paused or self._cleanup_paused or self._suspended_reason is not None:
            return "paused"
        if not camera.main_stream_url:
            return "failed"
        if self._settings.testing and self._recorder_factory is None:
            # No process is supervised in this environment, so there is nothing
            # to have failed. Report what was asked for rather than inventing a
            # fault the suite cannot cause.
            return "recording"
        return "failed"

    def is_recording(self, camera_id: str) -> bool:
        """Whether this camera's recorder process is alive right now.

        The honest answer, read from the process rather than from the setting
        that asked for it. A toggle that reports the setting back to the user is
        a toggle that cannot tell them it failed.
        """
        process = self._recorders.get(camera_id)
        return process is not None and process.is_running

    async def apply_camera_recording(self, camera: Camera) -> bool:
        """Start or stop exactly this camera's recorder, and nothing else.

        Returns whether the recorder is running afterwards.

        Deliberately not ``sync()``: reconciling the whole set to change one
        camera means a bug in an unrelated camera's source policy can tear down
        a recorder the administrator did not touch. Here the blast radius is one
        process.

        Raises whatever ``_start_recorder`` raises. A start that fails must not
        look like a start that worked.
        """
        camera_id = camera.id
        # Per-camera serialisation on top of the manager lock: two clicks on the
        # same toggle must not interleave a start and a stop, and the manager
        # lock alone would let the second caller run between them.
        lock = self._camera_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            wanted = self._should_record(camera)
            async with self._lock:
                running = camera_id in self._recorders
                if wanted and not running:
                    if self._settings.testing and self._recorder_factory is None:
                        # Nothing to spawn in the test environment unless a
                        # factory was installed, but the bookkeeping above still
                        # ran, so callers see a truthful "not running".
                        return False
                    try:
                        await self._start_recorder(camera)
                    except Exception:
                        # _start_recorder registers the process before starting
                        # it, so a failed start must not leave a dead entry that
                        # health reporting would go on counting.
                        self._recorders.pop(camera_id, None)
                        self._active_source.pop(camera_id, None)
                        self._active_profile.pop(camera_id, None)
                        raise
                elif running and not wanted:
                    await self._stop_recorder(camera_id)
            return self.is_recording(camera_id)

    def _policy_for(self, camera: Camera) -> SourcePolicy:
        """Build a policy view over this camera's persisted qualification state."""
        pinned_source = {
            RecordingSourceMode.RELAY: RecorderSource.RELAY,
            RecordingSourceMode.DIRECT: RecorderSource.DIRECT,
        }.get(camera.recording_source_mode)
        return SourcePolicy(
            relay_available=self._settings.record_via_go2rtc,
            direct_available=recorder.direct_input(camera) is not None,
            state=self._source_states.setdefault(camera.id, CameraSourceState()),
            pinned_source=pinned_source,
        )

    def _resolved_profile(self, camera: Camera, *, now: datetime) -> RecordingProfile:
        """Turn the camera's configured profile into the one to actually run."""
        snapshot = self._corruption.snapshot(camera.id, now=now)
        return profiles.resolve_profile(
            camera.recording_profile, corruption_observed=snapshot.is_material
        )

    async def _start_recorder(self, camera: Camera) -> None:
        root = self._root
        if root is None:
            return

        camera_id, camera_name = camera.id, camera.name
        settings = self._settings
        # A measured timestamp/keyframe pathology may require a shorter segment
        # on one camera. Keep the system default for every other recorder: a
        # global reduction can create unplayable cuts on cameras with a long
        # keyframe interval.
        segment_seconds = self._segment_seconds_for(camera)
        fps = camera.fps

        policy = self._policy_for(camera)

        def build_command() -> list[str]:
            # Rebuilt on every restart, so the directories for the current hour
            # exist even if the recorder has been down for a while, and so a
            # source or profile decision taken since the last start is honoured.
            moment = datetime.now(UTC)
            decision = policy.record_start(moment)
            source = recorder.input_for_source(camera, settings, decision.source)
            if source is None and camera.recording_source_mode is RecordingSourceMode.AUTO:
                # The decided source became unusable between decisions. Fall
                # back to whatever this install can still offer rather than
                # leaving the camera unrecorded.
                source = recorder.resolve_input(camera, settings)
            if source is None:
                raise ValueError(f"Camera {camera_name} has no usable stream URL.")

            self._active_source[camera_id] = source.source
            if decision.changed:
                # A source change invalidates the corruption evidence, which
                # described a different path entirely.
                self._corruption.reset(camera_id)
                logger.info(
                    "Recorder source changed",
                    extra={
                        "camera": camera_name,
                        "camera_id": camera_id,
                        "source": source.source.value,
                        "reason": decision.reason.value,
                    },
                )
            # Resolve only after resetting evidence from a different input.
            # Otherwise corruption seen on the abandoned source would make an
            # AUTO camera start its clean alternate in RESILIENT mode.
            profile = self._resolved_profile(camera, now=moment)
            self._active_profile[camera_id] = profile
            return recorder.build_record_command(
                source,
                recorder.prepare_output(root, camera_id),
                ffmpeg_binary=settings.ffmpeg_binary,
                segment_seconds=segment_seconds,
                profile=profile,
                fps=fps,
            )

        def on_output(line: str) -> bool:
            """Count recorder output instead of logging every line of it."""
            moment = datetime.now(UTC)
            kind = self._corruption.observe(camera_id, line, now=moment)
            if kind is None:
                return False
            if self._corruption.due_for_summary(camera_id, now=moment):
                snapshot = self._corruption.snapshot(camera_id, now=moment)
                logger.warning(
                    "Recorder input is delivering damaged media",
                    extra={
                        "camera": camera_name,
                        "camera_id": camera_id,
                        "events": snapshot.total,
                        "per_minute": round(snapshot.events_per_minute, 1),
                        "dominant": snapshot.dominant.value if snapshot.dominant else None,
                        "source": self._active_source.get(camera_id, RecorderSource.RELAY).value,
                    },
                )
            # Consumed: the periodic summary above is the whole report, which is
            # what turns 25,000 identical lines an hour into one counted fact.
            return True

        async def on_exit(return_code: int | None) -> None:
            # A graceful application shutdown also terminates FFmpeg with a
            # non-zero signal. consecutive_failures stays zero in that case, so
            # do not mistake an intentional stop for a bad camera input.
            outcome = SourceOutcome(
                source=self._active_source.get(camera_id, RecorderSource.RELAY),
                uptime_seconds=process.last_uptime_seconds or 0.0,
                return_code=return_code,
                # FFmpeg reaching EOF may exit zero, but it is still an
                # unexpected recording interruption. Only an explicit stop or
                # configuration restart is neutral to source qualification.
                counts_as_failure=not process.last_exit_expected,
            )
            decision = policy.record_outcome(outcome, datetime.now(UTC))
            await self._save_source_states()
            if decision.changed:
                logger.warning(
                    "Recorder input failed qualification; switching source",
                    extra={
                        "camera": camera_name,
                        "camera_id": camera_id,
                        "source": decision.source.value,
                        "reason": decision.reason.value,
                    },
                )

        factory = self._recorder_factory or _supervised_recorder
        process = factory(
            f"recorder:{camera_name}",
            build_command,
            recorder.build_recorder_environment(),
            on_exit,
            on_output,
        )
        self._recorders[camera_id] = process
        await process.start()
        logger.info(
            "Recording started",
            extra={
                "camera": camera_name,
                "camera_id": camera_id,
                "segment_seconds": segment_seconds,
            },
        )
        publish(
            RECORDING_STARTED,
            {"camera_id": camera_id, "camera": camera_name, "segment_seconds": segment_seconds},
        )

    def _segment_seconds_for(self, camera: Camera) -> int:
        """Resolve the camera override without changing the system default."""
        return camera.recording_segment_seconds or self._segment_seconds

    async def _save_source_states(self) -> None:
        """Persist learned qualification. Camera identifiers only, never a URL.

        Bounded by construction: one small record per camera, pruned to the
        cameras that still exist.
        """
        async with session_scope() as session:
            cameras = await list_cameras(session)
            self._source_states = prune_states(
                self._source_states, {camera.id for camera in cameras}
            )
            await settings_store.set_setting(
                session,
                settings_store.RECORDER_SOURCE_POLICY,
                encode_states(self._source_states),
            )

    async def reset_source(self, camera_id: str) -> SourceReason:
        """Clear a camera's learned source penalties and requalify from home.

        The manual escape hatch: an administrator who has just fixed a camera
        or a network should not have to wait out an hour-long cooldown.
        """
        async with session_scope() as session:
            cameras = {camera.id: camera for camera in await list_cameras(session)}
        camera = cameras.get(camera_id)
        if camera is None:
            raise LookupError(camera_id)
        policy = self._policy_for(camera)
        decision = policy.reset(datetime.now(UTC))
        self._corruption.reset(camera_id)
        await self._save_source_states()
        logger.info(
            "Recorder source reset by an administrator",
            extra={"camera_id": camera_id, "source": decision.source.value},
        )
        return decision.reason

    async def _stop_recorder(self, camera_id: str) -> None:
        process = self._recorders.get(camera_id)
        if process is None:
            return
        # Keep ownership until stop really returns. Removing the process first
        # made a failed stop look like success to the API and health probe even
        # though FFmpeg could still be writing footage.
        await process.stop()
        if process.is_running:
            raise RuntimeError("Recorder process is still running after stop.")
        if self._recorders.get(camera_id) is process:
            self._recorders.pop(camera_id, None)
        self._active_source.pop(camera_id, None)
        self._active_profile.pop(camera_id, None)
        logger.info("Recording stopped", extra={"camera_id": camera_id})
        publish(RECORDING_STOPPED, {"camera_id": camera_id})

    async def _stop_all(self, reason: str) -> None:
        async with self._lock:
            if not self._recorders:
                return
            logger.error("Suspending all recording", extra={"detail": reason})
            publish(STORAGE_CRITICAL, {"reason": reason})
            await asyncio.gather(
                *(process.stop() for process in self._recorders.values()),
                return_exceptions=True,
            )
            self._recorders.clear()
            self._forget_runtime()

    def _forget_runtime(self) -> None:
        """Drop per-run state. Learned qualification deliberately survives."""
        self._active_source.clear()
        self._active_profile.clear()

    # --- Background loops -------------------------------------------------

    async def _directory_loop(self) -> None:
        while True:
            try:
                await self._ensure_directories()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Could not pre-create recording directories", exc_info=True)
            await asyncio.sleep(DIRECTORY_TICK_SECONDS)

    async def _ensure_directories(self) -> None:
        root = self._root
        if root is None or self._suspended_reason is not None:
            return
        now = datetime.now(UTC)
        for camera_id in list(self._recorders):
            await asyncio.to_thread(paths.ensure_hour_directories, root, camera_id, now)

    async def _index_loop(self) -> None:
        while True:
            try:
                await self.index_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Segment indexing failed", exc_info=True)
            await asyncio.sleep(self._settings.index_interval_seconds)

    async def _recorder_watch_loop(self) -> None:
        while True:
            await asyncio.sleep(RECORDER_WATCH_TICK_SECONDS)
            try:
                await self._restart_stalled_recorders()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Recorder write-liveness check failed", exc_info=True)

    async def _restart_stalled_recorders(self) -> None:
        """Reconnect FFmpeg processes that are alive but have stopped writing."""
        root = self._root
        if root is None or self._suspended_reason is not None:
            return

        now = datetime.now(UTC)
        stall_seconds = max(RECORDER_STALL_MIN_SECONDS, self._segment_seconds * 2.0)
        stale_before = now.timestamp() - stall_seconds

        async with self._lock:
            for camera_id, process in self._recorders.items():
                started_at = process.started_at
                if (
                    not process.is_running
                    or not isinstance(started_at, datetime)
                    or (now - started_at).total_seconds() < stall_seconds
                ):
                    continue

                latest_write = await asyncio.to_thread(
                    self._latest_segment_write_time, root, camera_id, now
                )
                if latest_write is not None and latest_write >= stale_before:
                    continue

                logger.warning(
                    "Recorder is alive but not writing; reconnecting input",
                    extra={
                        "camera_id": camera_id,
                        "stalled_seconds": round(stall_seconds),
                    },
                )
                await process.restart()

    @staticmethod
    def _latest_segment_write_time(root: Path, camera_id: str, now: datetime) -> float | None:
        directory = paths.hour_directory(root, camera_id, now)
        if not directory.is_dir():
            return None
        mtimes = (
            path.stat().st_mtime
            for path in directory.glob(f"*{paths.SEGMENT_SUFFIX}")
            if path.is_file()
        )
        return max(mtimes, default=None)

    async def index_once(self) -> indexer.IndexResult:
        """One indexing sweep across every camera."""
        root = self._root
        if root is None:
            return indexer.IndexResult()
        async with self._maintenance_lock, session_scope() as session:
            cameras = await list_cameras(session)
            return await indexer.index_all(
                session,
                [c.id for c in cameras if c.kind is CameraKind.RTSP],
                root,
            )

    async def _retention_loop(self) -> None:
        while True:
            # Deliberately after a delay rather than before: a restart should
            # not begin by deleting, so a misconfigured policy is visible in the
            # interface before it acts.
            await asyncio.sleep(RETENTION_TICK_SECONDS)
            try:
                await self.run_retention()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Retention sweep failed", exc_info=True)

    async def run_retention(self) -> retention.SweepResult:
        root = self._root
        if root is None or self._cleanup_paused or self._suspended_reason is not None:
            return retention.SweepResult(reason="Storage is unavailable; retention skipped.")
        async with self._maintenance_lock, session_scope() as session:
            return await retention.sweep(
                session, root, storage=self._storage, settings=self._settings
            )

    async def _storage_loop(self) -> None:
        while True:
            await asyncio.sleep(STORAGE_TICK_SECONDS)
            try:
                await self._check_storage()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Storage check failed", exc_info=True)

    async def _check_storage(self) -> None:
        root = self._root
        if root is None:
            return

        status = await asyncio.to_thread(check_storage, root)
        self._storage = status

        if not status.available and self._suspended_reason is None:
            self._suspended_reason = status.reason
            await self._stop_all(status.reason or "Storage is unavailable.")
        elif status.available and self._suspended_reason is not None:
            logger.info("Storage is available again; resuming recording")
            self._suspended_reason = None
            await self.sync()

    # --- Reporting -------------------------------------------------------

    @property
    def suspended_reason(self) -> str | None:
        return self._suspended_reason

    @property
    def storage(self) -> StorageStatus | None:
        return self._storage

    @property
    def segment_seconds(self) -> int:
        """The configured segment length, as loaded at start-up."""
        return self._segment_seconds

    def recorder_source(self, camera_id: str) -> RecorderSource | None:
        """Which input this camera's recorder is currently reading from.

        ``None`` when no recorder is supervised for the camera. Only ever a
        relay/direct label: the URL and any credentials in it stay inside the
        input resolution.
        """
        if camera_id not in self._recorders:
            return None
        return self._active_source.get(camera_id)

    def recorder_profile(self, camera_id: str) -> RecordingProfile | None:
        """The concrete profile this camera's recorder was started with."""
        if camera_id not in self._recorders:
            return None
        return self._active_profile.get(camera_id)

    def source_reason(self, camera_id: str) -> SourceReason | None:
        """Why the current source was chosen, for the administrator panel."""
        state = self._source_states.get(camera_id)
        return state.reason if state is not None else None

    def source_state(self, camera_id: str) -> CameraSourceState | None:
        return self._source_states.get(camera_id)

    def corruption_snapshot(
        self, camera_id: str, *, now: datetime | None = None
    ) -> corruption.CorruptionSnapshot:
        """Counted media damage for one camera. Counts only, never text."""
        return self._corruption.snapshot(camera_id, now=now or datetime.now(UTC))

    def recorder_status(self) -> list[dict[str, object]]:
        now = datetime.now(UTC)
        rows: list[dict[str, object]] = []
        for camera_id, process in self._recorders.items():
            source = self._active_source.get(camera_id)
            profile = self._active_profile.get(camera_id)
            state = self._source_states.get(camera_id)
            damage = self._corruption.snapshot(camera_id, now=now)
            rows.append(
                {
                    "camera_id": camera_id,
                    "source": source.value if source else None,
                    "profile": profile.value if profile else None,
                    "source_reason": state.reason.value if state else None,
                    "source_switches": state.switch_count if state else 0,
                    "source_qualified": bool(state.qualified) if state else False,
                    "corruption_events": damage.total,
                    "corruption_per_minute": round(damage.events_per_minute, 2),
                    "corruption_kind": damage.dominant.value if damage.dominant else None,
                    **process.status(),
                }
            )
        return rows

    async def health_probe(self) -> health.ComponentHealth:
        if self._privacy_paused:
            return health.ComponentHealth(
                state=health.ComponentState.SKIPPED,
                detail="Paused by privacy mode.",
                data={"expected": 0, "running": 0},
            )

        if self._cleanup_paused:
            return health.ComponentHealth(
                state=health.ComponentState.SKIPPED,
                detail="Recording is briefly paused while stored footage is being removed.",
                data={"expected": 0, "running": 0},
            )

        if self._suspended_reason is not None:
            return health.ComponentHealth(
                state=health.ComponentState.FAILED, detail=self._suspended_reason
            )

        async with session_scope() as session:
            cameras = await list_cameras(session)
        expected = [camera for camera in cameras if self._should_record(camera)]

        if not expected:
            return health.ComponentHealth(
                state=health.ComponentState.SKIPPED,
                detail="No cameras are set to record.",
            )

        running = sum(1 for process in self._recorders.values() if process.is_running)
        data: dict[str, object] = {"expected": len(expected), "running": running}

        if running == 0:
            return health.ComponentHealth(
                state=health.ComponentState.FAILED,
                detail="No recorder is running.",
                data=data,
            )
        if running < len(expected):
            return health.ComponentHealth(
                state=health.ComponentState.DEGRADED,
                detail=f"{len(expected) - running} of {len(expected)} recorders are down.",
                data=data,
            )
        return health.ComponentHealth(state=health.ComponentState.OK, data=data)


_manager: RecordingManager | None = None


def get_manager() -> RecordingManager:
    global _manager
    if _manager is None:
        _manager = RecordingManager()
    return _manager


def set_recorder_factory(factory: RecorderFactory | None) -> None:
    """Install a recorder process factory on the live manager (test seam)."""
    get_manager()._recorder_factory = factory


def set_manager(manager: RecordingManager | None) -> None:
    """Test helper."""
    global _manager
    _manager = manager
