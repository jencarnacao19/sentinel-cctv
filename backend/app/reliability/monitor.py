"""The Reliability Center's runtime: gather telemetry, decide, record, recover.

This module owns two things and deliberately no more.

**Observation.** Every tick it collects what the recording, motion, streaming
and privacy managers already know, asks :mod:`app.reliability.state` what that
means, and reconciles the persisted incident list. It opens no sockets, spawns
no processes and reads no directories, so adding it does not change how many
clients a camera sees.

**One explicit recovery action.** Sentinel already restarts failed processes by
itself, with backoff, for ever. A second automatic restarter competing with the
first would produce exactly the thrash the supervisor exists to avoid, so there
is none here. What there is instead is a single administrator-initiated
"retry", which reuses the existing manager synchronisation entry points for one
camera, is rate limited per camera regardless of who asks, is refused outright
while privacy mode is active, and reports what it did rather than claiming the
video came back.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.cameras.service import list_cameras
from app.config import Settings, get_settings
from app.database.models import (
    Camera,
    CameraIncident,
    CameraKind,
    IncidentReason,
    RecorderSource,
    RecordingMode,
    RecordingProfile,
)
from app.database.session import session_scope
from app.logging_config import get_logger
from app.recording import corruption as corruption_module
from app.recording.source_policy import SourceReason
from app.reliability import service
from app.reliability.faults import classify
from app.reliability.state import (
    COVERAGE_WINDOW_SECONDS,
    RECORDING_MODES,
    Assessment,
    CameraState,
    CameraTelemetry,
    assess,
    freshness_limit,
    worst,
)
from app.streaming.config import stream_names
from app.supervisor import ProcessState
from app.system import privacy

logger = get_logger("SYSTEM")

#: How often the background evaluation runs. Fast enough that an outage is
#: visible within one screen refresh, slow enough that eight cameras cost one
#: indexed query every half minute.
TICK_SECONDS = 30.0

#: A fault must persist this long before an incident is opened. Below this a
#: normal supervisor reconnect -- the thing that is *supposed* to happen when a
#: camera hiccups -- would fill the history with rows nobody needs.
OPEN_AFTER_SECONDS = 90.0

#: Recovery must hold this long before an incident is closed. Longer than the
#: open window on purpose: closing early and reopening is the flapping this is
#: meant to prevent.
RECOVER_AFTER_SECONDS = 180.0

#: Minimum gap between manual retries of the same camera, whoever asks. The
#: per-client rate limit in the router protects Sentinel; this protects the
#: camera from two administrators pressing the button at once.
RETRY_COOLDOWN_SECONDS = 60.0

#: Retention is enforced on this cadence rather than every tick; the work is
#: two DELETEs and does not need to run twice a minute.
PRUNE_INTERVAL_SECONDS = 3600.0

#: Restart observations kept per camera. The window only ever needs the count,
#: and a camera restarting more than this in an hour is already far past every
#: threshold, so the cap costs nothing and bounds the memory.
MAX_TRACKED_RESTARTS = 256


class RecoveryThrottledError(RuntimeError):
    """A manual retry arrived inside this camera's cooldown window."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("This camera was retried very recently.")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What a manual retry actually did.

    ``actions`` and ``skipped`` are fixed identifiers, not prose, so the
    interface can word them and the API stays stable. Neither ever contains a
    host, a URL or a process command line.
    """

    camera_id: str
    requested_at: datetime
    actions: tuple[str, ...]
    skipped: tuple[str, ...]
    state_before: CameraState


@dataclass(frozen=True, slots=True)
class CameraReport:
    """One camera's row in the Reliability Center."""

    camera_id: str
    camera_name: str
    assessment: Assessment
    telemetry: CameraTelemetry
    active_incident: CameraIncident | None
    recent_incidents: tuple[CameraIncident, ...]
    last_healthy_at: datetime | None
    freshness_limit_seconds: float
    #: Recording-quality facts the manager owns rather than the assessment.
    #: Carried on the report so the API layer never reaches into a manager.
    profile: RecordingProfile | None = None
    source_reason: SourceReason | None = None
    source_switches: int = 0
    corruption_events: int = 0
    corruption_kind: str | None = None


@dataclass(frozen=True, slots=True)
class ReliabilitySnapshot:
    generated_at: datetime
    privacy_enabled: bool
    privacy_message: str | None
    storage_available: bool
    relay_expected: bool
    relay_ready: bool
    cameras: tuple[CameraReport, ...] = field(default_factory=tuple)

    def count(self, state: CameraState) -> int:
        return sum(1 for report in self.cameras if report.assessment.state is state)


class ReliabilityMonitor:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._task: asyncio.Task[None] | None = None

        # Debounce bookkeeping. Deliberately in memory: it describes a run of
        # observations, not a fact worth surviving a restart. After a restart a
        # persistent fault simply re-earns its incident, while an incident that
        # was already open is still in the database and closes normally.
        self._fault_since: dict[str, datetime] = {}
        self._pending_reason: dict[str, IncidentReason] = {}
        self._clear_since: dict[str, datetime] = {}
        self._last_healthy: dict[str, datetime] = {}
        self._restart_seen: dict[str, int] = {}
        self._pending_restarts: dict[str, int] = {}

        #: When each supervisor restart was observed, per camera, for the
        #: restart-loop rule. Bounded and pruned to the coverage window.
        self._restart_history: dict[str, deque[datetime]] = {}
        self._last_retry: dict[str, datetime] = {}
        self._last_prune: datetime | None = None
        self._lock = asyncio.Lock()

    # --- Lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._settings.testing:
            # Tests drive evaluate() directly with an explicit clock, which is
            # the only way to exercise a ninety-second debounce in a unit test.
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="reliability-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await self.evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Reporting must never be able to take recording down with it.
                logger.warning("Reliability evaluation failed", exc_info=True)

    # --- Observation -----------------------------------------------------

    async def snapshot(
        self, session: AsyncSession, *, now: datetime | None = None
    ) -> ReliabilitySnapshot:
        """Current state, without touching the incident list.

        Used by the API so that reading a status page never writes to the
        database and never shifts a debounce window.
        """
        return await self._build(session, now=now or datetime.now(UTC), reconcile=False)

    async def evaluate(self, *, now: datetime | None = None) -> ReliabilitySnapshot:
        """Current state, reconciling incidents. Driven by the tick loop."""
        moment = now or datetime.now(UTC)
        async with self._lock, session_scope() as session:
            return await self._build(session, now=moment, reconcile=True)

    async def _build(
        self, session: AsyncSession, *, now: datetime, reconcile: bool
    ) -> ReliabilitySnapshot:
        from app.motion.manager import get_manager as get_motion_manager
        from app.recording.manager import get_manager as get_recording_manager
        from app.streaming.manager import get_manager as get_streaming_manager

        recording = get_recording_manager()
        motion = get_motion_manager()
        streaming = get_streaming_manager()
        privacy_state = privacy.get_manager().state

        cameras = [
            camera for camera in await list_cameras(session) if camera.kind is CameraKind.RTSP
        ]
        camera_ids = [camera.id for camera in cameras]

        recorders = {str(status.get("camera_id")): status for status in recording.recorder_status()}
        workers = {str(status.get("camera_id")): status for status in motion.worker_status()}
        last_segments = await service.last_segment_times(session, camera_ids, now=now)
        coverage_starts: dict[str, datetime] = {}
        for camera in cameras:
            # ``created_at`` is the production authority. The recorder start is
            # also considered when it predates it so deterministic tests (and
            # imported legacy rows) can describe an already-running camera
            # without rewriting creation metadata.
            candidates = [camera.created_at]
            recorder_started = recorders.get(camera.id, {}).get("started_at")
            if isinstance(recorder_started, datetime):
                candidates.append(recorder_started)
            coverage_starts[camera.id] = min(candidates)
        # One extra indexed read per tick, over the same
        # (camera_id, started_at) index the freshness query already uses. This
        # is what turns "a segment arrived recently" into "the hour is actually
        # covered", which is the difference between the previous healthy
        # verdict on a 50%-coverage camera and an honest one.
        coverage = await service.segment_coverage(
            session,
            camera_ids,
            now=now,
            window_seconds=COVERAGE_WINDOW_SECONDS,
            window_starts=coverage_starts,
        )
        open_incidents = await service.active_incidents(session)
        recent = await service.history(session, camera_ids)

        storage = recording.storage
        storage_available = recording.suspended_reason is None and bool(
            storage is None or storage.available
        )
        # go2rtc is the live-view relay even when the operator runs it outside
        # Sentinel and recorders use direct RTSP. "Managed" describes process
        # ownership, not whether the UI depends on the relay.
        relay_expected = not privacy_state.enabled and any(camera.enabled for camera in cameras)
        relay_ready = streaming.is_ready

        reports: list[CameraReport] = []
        for camera in cameras:
            telemetry = self._telemetry(
                camera,
                recorder=recorders.get(camera.id),
                worker=workers.get(camera.id),
                last_segment_at=last_segments.get(camera.id),
                coverage=coverage.get(camera.id),
                restarts_in_window=self._restarts_in_window(camera.id, now=now),
                corruption=(damage := recording.corruption_snapshot(camera.id, now=now)),
                privacy_enabled=privacy_state.enabled,
                storage_available=storage_available,
                relay_expected=relay_expected,
                relay_ready=relay_ready,
                segment_seconds=recording.segment_seconds,
            )
            incident = open_incidents.get(camera.id)
            assessment = assess(telemetry, now=now, incident_open=incident is not None)

            if reconcile:
                restart_increment = self._restart_increment(
                    camera.id,
                    telemetry.restart_count,
                    frozen=telemetry.privacy_enabled,
                )
                # Recorded whether or not an incident is open: the restart-loop
                # rule needs the rate over the whole window, not only the part
                # of it that already had an incident attached.
                self._note_restarts(camera.id, restart_increment, now=now)
                incident = await self._reconcile(
                    session,
                    telemetry=telemetry,
                    assessment=assessment,
                    incident=incident,
                    restart_increment=restart_increment,
                    now=now,
                )
                # Opening or closing an incident changes what "recovering"
                # means for this camera, so the verdict is taken again rather
                # than reported one tick out of date.
                assessment = assess(telemetry, now=now, incident_open=incident is not None)

            if assessment.state is CameraState.HEALTHY:
                self._last_healthy[camera.id] = now

            history = tuple(recent.get(camera.id, ()))
            reports.append(
                CameraReport(
                    camera_id=camera.id,
                    camera_name=camera.name,
                    assessment=assessment,
                    telemetry=telemetry,
                    active_incident=incident,
                    recent_incidents=history,
                    last_healthy_at=self._last_healthy_at(camera.id, history),
                    freshness_limit_seconds=freshness_limit(
                        segment_seconds=telemetry.segment_seconds,
                        index_interval_seconds=telemetry.index_interval_seconds,
                    ),
                    profile=recording.recorder_profile(camera.id),
                    source_reason=recording.source_reason(camera.id),
                    source_switches=(
                        state.switch_count
                        if (state := recording.source_state(camera.id)) is not None
                        else 0
                    ),
                    corruption_events=damage.total,
                    corruption_kind=damage.dominant.value if damage.dominant else None,
                )
            )

        if reconcile:
            self._forget(camera_ids)
            await self._prune_if_due(session, now=now)

        return ReliabilitySnapshot(
            generated_at=now,
            privacy_enabled=privacy_state.enabled,
            privacy_message=privacy_state.message,
            storage_available=storage_available,
            relay_expected=relay_expected,
            relay_ready=relay_ready,
            cameras=tuple(reports),
        )

    def _telemetry(
        self,
        camera: Camera,
        *,
        recorder: dict[str, Any] | None,
        worker: dict[str, Any] | None,
        last_segment_at: datetime | None,
        coverage: service.CoverageWindow | None,
        restarts_in_window: int,
        corruption: corruption_module.CorruptionSnapshot,
        privacy_enabled: bool,
        storage_available: bool,
        relay_expected: bool,
        relay_ready: bool,
        segment_seconds: int,
    ) -> CameraTelemetry:
        state = _process_state(recorder.get("state") if recorder else None)
        started_at = recorder.get("started_at") if recorder else None
        return CameraTelemetry(
            camera_id=camera.id,
            camera_name=camera.name,
            enabled=camera.enabled,
            recording_enabled=camera.recording_enabled,
            recording_mode=camera.recording_mode,
            motion_enabled=camera.motion_enabled,
            privacy_enabled=privacy_enabled,
            storage_available=storage_available,
            recorder_present=recorder is not None,
            recorder_state=state,
            recorder_started_at=started_at if isinstance(started_at, datetime) else None,
            restart_count=int(recorder.get("restart_count") or 0) if recorder else 0,
            consecutive_failures=(
                int(recorder.get("consecutive_failures") or 0) if recorder else 0
            ),
            last_exit_code=recorder.get("last_exit_code") if recorder else None,
            # Only the classification crosses this boundary. The recorder's own
            # last line stays inside the supervisor, where diagnostics can
            # still reach it under its existing redaction.
            fault=classify(recorder.get("last_error") if recorder else None),
            source=_recorder_source(recorder),
            last_segment_at=last_segment_at,
            motion_expected=camera.enabled and camera.motion_enabled and not privacy_enabled,
            motion_running=bool(worker and worker.get("running")),
            relay_expected=relay_expected,
            relay_ready=relay_ready,
            segment_seconds=segment_seconds,
            index_interval_seconds=self._settings.index_interval_seconds,
            coverage_ratio=coverage.ratio if coverage is not None else None,
            coverage_segments=coverage.segments if coverage is not None else 0,
            coverage_window_seconds=(
                coverage.window_seconds if coverage is not None else COVERAGE_WINDOW_SECONDS
            ),
            coverage_gap_count=coverage.gap_count if coverage is not None else 0,
            coverage_longest_gap_seconds=(
                coverage.longest_gap_seconds if coverage is not None else 0.0
            ),
            damaged_segments=coverage.damaged_segments if coverage is not None else 0,
            short_segments=coverage.short_segments if coverage is not None else 0,
            restarts_in_window=restarts_in_window,
            corruption_per_minute=corruption.events_per_minute,
        )

    def _restarts_in_window(self, camera_id: str, *, now: datetime) -> int:
        """Supervisor restarts observed inside the coverage window.

        The supervised process only exposes a monotonic total, so the monitor
        keeps its own small ring of observation times. Bounded by construction:
        entries older than the window are dropped on every read.
        """
        history = self._restart_history.get(camera_id)
        if not history:
            return 0
        cutoff = now - timedelta(seconds=COVERAGE_WINDOW_SECONDS)
        while history and history[0] < cutoff:
            history.popleft()
        return len(history)

    def _note_restarts(self, camera_id: str, count: int, *, now: datetime) -> None:
        if count <= 0:
            return
        history = self._restart_history.setdefault(camera_id, deque(maxlen=MAX_TRACKED_RESTARTS))
        for _ in range(min(count, MAX_TRACKED_RESTARTS)):
            history.append(now)

    def _last_healthy_at(
        self, camera_id: str, history: tuple[CameraIncident, ...]
    ) -> datetime | None:
        """The most recent moment this camera is known to have been healthy.

        Two sources, both provable. What this process has observed, and the
        recovery time of the newest closed incident -- an incident only closes
        after recovery has been sustained, so its ``resolved_at`` is a real
        healthy moment even if it happened before the last restart.
        """
        observed = self._last_healthy.get(camera_id)
        recovered = next(
            (incident.resolved_at for incident in history if incident.resolved_at is not None),
            None,
        )
        if observed is None:
            return recovered
        if recovered is None:
            return observed
        return max(observed, recovered)

    # --- Incident reconciliation -----------------------------------------

    async def _reconcile(
        self,
        session: AsyncSession,
        *,
        telemetry: CameraTelemetry,
        assessment: Assessment,
        incident: CameraIncident | None,
        restart_increment: int,
        now: datetime,
    ) -> CameraIncident | None:
        camera_id = telemetry.camera_id
        reason = assessment.reason

        if telemetry.privacy_enabled:
            # Privacy mode freezes the incident list rather than resolving it.
            # Closing an incident here would record a recovery that never
            # happened, and opening one would turn a deliberate pause into an
            # outage. Whatever was true when privacy started is still true when
            # it ends, and the normal recovery grace takes over from there.
            self._fault_since.pop(camera_id, None)
            self._pending_reason.pop(camera_id, None)
            self._clear_since.pop(camera_id, None)
            return incident

        if reason is None:
            self._fault_since.pop(camera_id, None)
            self._pending_reason.pop(camera_id, None)
            self._pending_restarts.pop(camera_id, None)
            if incident is None:
                self._clear_since.pop(camera_id, None)
                return None
            if restart_increment > 0:
                incident.restart_count += restart_increment
            started = self._clear_since.setdefault(camera_id, now)
            if (now - started).total_seconds() < RECOVER_AFTER_SECONDS:
                return incident
            await service.close_incident(session, incident, resolved_at=now)
            self._clear_since.pop(camera_id, None)
            return None

        # Something is wrong. A run of clean observations is over.
        self._clear_since.pop(camera_id, None)
        pending = worst(self._pending_reason.get(camera_id), reason) or reason
        self._pending_reason[camera_id] = pending

        if incident is not None:
            service.update_incident(
                incident,
                reason=reason,
                detail=assessment.detail,
                fault=assessment.fault,
                source=assessment.source,
                restart_increment=restart_increment,
                coverage_ratio=telemetry.coverage_ratio,
                damaged_segments=telemetry.damaged_segments,
            )
            await session.flush()
            return incident

        first_seen = self._fault_since.setdefault(camera_id, now)
        if restart_increment > 0:
            self._pending_restarts[camera_id] = (
                self._pending_restarts.get(camera_id, 0) + restart_increment
            )
        if (now - first_seen).total_seconds() < OPEN_AFTER_SECONDS:
            return None

        opened = await service.open_incident(
            session,
            camera_id,
            reason=pending,
            detail=assessment.detail,
            fault=assessment.fault,
            source=assessment.source,
            started_at=first_seen,
            restart_count=self._pending_restarts.pop(camera_id, 0),
            coverage_ratio=telemetry.coverage_ratio,
            damaged_segments=telemetry.damaged_segments,
        )
        self._fault_since.pop(camera_id, None)
        self._pending_reason.pop(camera_id, None)
        return opened

    def _restart_increment(self, camera_id: str, counter: int, *, frozen: bool) -> int:
        """Turn a supervisor lifetime counter into restarts observed per tick.

        A manual retry rebuilds the supervised process and resets its counter.
        Treating a lower value as a new counter lifetime keeps later increments
        countable without inventing a restart at the reset itself.
        """
        current = max(counter, 0)
        previous = self._restart_seen.get(camera_id)
        if frozen:
            return 0
        self._restart_seen[camera_id] = current
        if previous is None:
            return 0
        if current >= previous:
            return current - previous
        return current

    def _forget(self, camera_ids: list[str]) -> None:
        """Drop bookkeeping for cameras that no longer exist."""
        known = set(camera_ids)
        for table in (
            self._fault_since,
            self._pending_reason,
            self._clear_since,
            self._last_healthy,
            self._last_retry,
            self._restart_seen,
            self._pending_restarts,
            self._restart_history,
        ):
            for camera_id in [key for key in table if key not in known]:
                del table[camera_id]

    async def _prune_if_due(self, session: AsyncSession, *, now: datetime) -> None:
        if self._last_prune is not None and now - self._last_prune < timedelta(
            seconds=PRUNE_INTERVAL_SECONDS
        ):
            return
        self._last_prune = now
        await service.prune(session, now=now)

    # --- Manual recovery -------------------------------------------------

    async def retry_camera(self, camera: Camera, *, now: datetime | None = None) -> RecoveryOutcome:
        """Ask this camera's local services to restart. Nothing else.

        Explicitly not done here: rebooting the camera, contacting a vendor
        cloud, touching credentials or configuration, restarting go2rtc itself,
        or reporting success the recorder has not earned yet.
        """
        privacy.require_camera_access()

        moment = now or datetime.now(UTC)
        previous = self._last_retry.get(camera.id)
        if previous is not None:
            elapsed = (moment - previous).total_seconds()
            if elapsed < RETRY_COOLDOWN_SECONDS:
                raise RecoveryThrottledError(RETRY_COOLDOWN_SECONDS - elapsed)
        self._last_retry[camera.id] = moment

        from app.motion.manager import get_manager as get_motion_manager
        from app.recording.manager import get_manager as get_recording_manager
        from app.streaming.manager import get_manager as get_streaming_manager

        recording = get_recording_manager()
        state_before = await self._state_of(camera.id, now=moment)

        actions: list[str] = []
        skipped: list[str] = []

        main_name, sub_name = stream_names(camera)
        replace = {main_name} | ({sub_name} if sub_name else set())
        try:
            # Replaces only this camera's stream definitions. go2rtc keeps
            # serving every other camera, so one retry cannot interrupt seven
            # live views and seven recordings.
            await get_streaming_manager().sync(replace_streams=replace)
            # sync() deliberately treats an unavailable relay as a durable
            # config refresh for its next start. Calling this "reloaded" would
            # claim a network result the method does not guarantee.
            actions.append("relay_stream_refresh_requested")
        except Exception:
            logger.warning(
                "Could not reload the relay stream during manual recovery",
                extra={"camera_id": camera.id},
                exc_info=True,
            )
            skipped.append("relay_stream_unavailable")

        if recording.suspended_reason is not None:
            skipped.append("storage_unavailable")
        elif camera.recording_mode in RECORDING_MODES:
            try:
                await recording.restart_camera(camera.id)
                actions.append("recorder_restarted")
            except Exception:
                logger.warning(
                    "Could not restart the recorder during manual recovery",
                    extra={"camera_id": camera.id},
                    exc_info=True,
                )
                skipped.append("recorder_restart_failed")
        else:
            skipped.append("recording_not_configured")

        if camera.motion_enabled and camera.recording_mode is not RecordingMode.OFF:
            try:
                await get_motion_manager().restart_camera(camera.id)
                actions.append("motion_worker_restarted")
            except Exception:
                logger.warning(
                    "Could not restart the motion worker during manual recovery",
                    extra={"camera_id": camera.id},
                    exc_info=True,
                )
                skipped.append("motion_restart_failed")
        else:
            skipped.append("motion_not_configured")

        logger.info(
            "Manual camera recovery requested",
            extra={"camera_id": camera.id, "actions": ",".join(actions)},
        )
        return RecoveryOutcome(
            camera_id=camera.id,
            requested_at=moment,
            actions=tuple(actions),
            skipped=tuple(skipped),
            state_before=state_before,
        )

    async def _state_of(self, camera_id: str, *, now: datetime) -> CameraState:
        async with session_scope() as session:
            snapshot = await self.snapshot(session, now=now)
        for report in snapshot.cameras:
            if report.camera_id == camera_id:
                return report.assessment.state
        return CameraState.DISABLED


def _process_state(value: Any) -> ProcessState | None:
    if isinstance(value, ProcessState):
        return value
    if isinstance(value, str):
        try:
            return ProcessState(value)
        except ValueError:
            return None
    return None


def _recorder_source(recorder: dict[str, Any] | None) -> RecorderSource | None:
    if recorder is None:
        return None
    value = recorder.get("source")
    if isinstance(value, RecorderSource):
        return value
    if isinstance(value, str):
        try:
            return RecorderSource(value)
        except ValueError:
            return None
    return None


_monitor: ReliabilityMonitor | None = None


def get_monitor() -> ReliabilityMonitor:
    global _monitor
    if _monitor is None:
        _monitor = ReliabilityMonitor()
    return _monitor


def set_monitor(monitor: ReliabilityMonitor | None) -> None:
    """Test helper."""
    global _monitor
    _monitor = monitor
