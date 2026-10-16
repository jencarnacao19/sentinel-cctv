"""Deriving one camera's operational state from telemetry Sentinel already has.

Nothing in this module performs I/O. Everything it needs is passed in as a
:class:`CameraTelemetry` value, which is what makes the rules below testable
without a camera, a database or a running FFmpeg -- and what guarantees that
reliability monitoring never becomes an extra consumer of a camera.

The state vocabulary is deliberately small and each member means one thing:

``privacy_paused``
    An administrator paused every camera on purpose. Not an outage, and never
    an incident.
``disabled``
    This camera is switched off, or continuous recording is not selected for
    it. Nothing is expected to be running, so nothing is reported as wrong.
``healthy``
    Recording as configured, with a recent completed segment, and every
    supporting service this camera needs is up.
``degraded``
    Recording is fresh, but something around it is not: the local relay is not
    answering, or the motion worker is down.
``recovering``
    A fault is being worked on. Either the supervisor is mid-restart, or the
    fault has cleared and Sentinel is waiting to see the recovery hold before
    closing the incident.
``offline``
    Recording is expected and is not happening, with no recovery visibly in
    progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.database.models import IncidentReason, RecorderSource, RecordingMode, StreamFault
from app.supervisor import ProcessState


class CameraState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    RECOVERING = "recovering"
    DISABLED = "disabled"
    PRIVACY_PAUSED = "privacy_paused"


#: Recording modes that mean "a recorder should be running for this camera".
#: Mirrors app.recording.manager.CONTINUOUS_MODES; motion-only and scheduled
#: recording are not production modes yet, so a camera set to one of them is
#: reported as not recording rather than as broken.
RECORDING_MODES = frozenset({RecordingMode.CONTINUOUS})

#: Consecutive supervisor failures after which a restart cycle stops being
#: "recovering" and becomes an honest "offline". The backoff ladder is
#: 2s/5s/10s/30s/60s, so three failures means the camera has been unreachable
#: for roughly twenty seconds of real attempts, not one unlucky reconnect.
OFFLINE_AFTER_FAILURES = 3

#: Added on top of the recorder's own stall window before a missing segment is
#: called a fault. Covers the gap between FFmpeg closing a file and the indexer
#: noticing it, so a healthy camera is never flagged as stale.
FRESHNESS_MARGIN_SECONDS = 60.0

#: The recorder's write-liveness watchdog gives FFmpeg two full segment windows,
#: with this floor, before reconnecting it. Reliability uses the same figure so
#: the two subsystems cannot disagree about what "stalled" means.
RECORDER_STALL_MIN_SECONDS = 120.0

#: Window over which recorded continuity is measured.
#:
#: One hour is long enough that a single restart cannot drag a healthy camera
#: below the threshold, and short enough that an operator sees a camera that
#: started failing this morning rather than one that averaged well overnight.
COVERAGE_WINDOW_SECONDS = 3600.0

#: A camera must record at least this fraction of the window. The reference
#: installation's healthy channels measured 99.4%; the two broken ones measured
#: 80.4% and 50.5%, and both reported "healthy" because each had written a
#: segment in the last three minutes. Ninety-five per cent sits well clear of
#: normal restart noise and well below anything an operator would accept.
MIN_COVERAGE_RATIO = 0.95

#: Coverage below this is not merely degraded -- most of the footage is simply
#: not there, which is an outage whatever the process table says.
CRITICAL_COVERAGE_RATIO = 0.75

#: Coverage is only judged once the window holds enough measured segments to
#: mean anything. A camera enabled ten minutes ago has not had the chance to
#: cover an hour, and reporting it as failing would be false.
MIN_COVERAGE_SEGMENTS = 10

#: Supervisor restarts within the coverage window above which a camera counts
#: as looping rather than as having had one bad reconnect. The reference
#: installation's worst camera restarted 60 times in four hours; a healthy one
#: restarts zero or once.
RESTART_LOOP_THRESHOLD = 4

#: Corruption events per minute above which the source is called corrupt.
#: Mirrors ``app.recording.corruption.CORRUPTION_RATE_THRESHOLD_PER_MINUTE``.
CORRUPTION_RATE_THRESHOLD_PER_MINUTE = 6.0

#: How severe each reason is, most severe first. Used when a camera produces
#: several different faults during one outage: the incident keeps the worst one
#: seen rather than flapping between "restarting" and "not fresh" as the
#: supervisor cycles.
REASON_SEVERITY: tuple[IncidentReason, ...] = (
    IncidentReason.STORAGE_UNAVAILABLE,
    IncidentReason.RECORDER_UNAVAILABLE,
    IncidentReason.STREAM_SOURCE_FAILURE,
    IncidentReason.RECORDING_NOT_FRESH,
    # Continuity ranks just below "no footage at all": a camera recording half
    # the hour is worse than one whose recorder is merely mid-reconnect.
    IncidentReason.RECORDING_COVERAGE_LOW,
    IncidentReason.RECORDING_UNSTABLE,
    IncidentReason.STREAM_CORRUPTION,
    IncidentReason.RECORDER_RESTARTING,
    IncidentReason.RELAY_UNAVAILABLE,
    IncidentReason.MOTION_WORKER_UNAVAILABLE,
)

REASON_LABELS: dict[IncidentReason, str] = {
    IncidentReason.STORAGE_UNAVAILABLE: "Recording storage unavailable",
    IncidentReason.RECORDER_UNAVAILABLE: "No recorder running",
    IncidentReason.STREAM_SOURCE_FAILURE: "Camera stream failing",
    IncidentReason.RECORDING_NOT_FRESH: "Recording not fresh",
    IncidentReason.RECORDING_COVERAGE_LOW: "Recording is not continuous",
    IncidentReason.RECORDING_UNSTABLE: "Recorder keeps restarting",
    IncidentReason.STREAM_CORRUPTION: "Camera stream is corrupt",
    IncidentReason.RECORDER_RESTARTING: "Recorder reconnecting",
    IncidentReason.RELAY_UNAVAILABLE: "Local stream relay unavailable",
    IncidentReason.MOTION_WORKER_UNAVAILABLE: "Motion detection stopped",
}

FAULT_LABELS: dict[StreamFault, str] = {
    StreamFault.MISSING_STREAM_PARAMETERS: (
        "The stream did not repeat its H.264 setup data, so the recorder could "
        "not determine the picture size."
    ),
    StreamFault.CORRUPT_PACKETS: "The camera stream arrived damaged or out of order.",
    StreamFault.CONNECTION_FAILED: "The recorder could not open the camera stream.",
    StreamFault.TIMEOUT: "The camera stream stopped responding.",
    StreamFault.AUTHENTICATION_REJECTED: "The camera rejected the stored credentials.",
    StreamFault.UNKNOWN: "The recorder reported an error Sentinel does not recognise.",
}


def severity(reason: IncidentReason | None) -> int:
    """Lower is worse. ``None`` sorts after every real reason."""
    if reason is None:
        return len(REASON_SEVERITY)
    return REASON_SEVERITY.index(reason)


def worst(first: IncidentReason | None, second: IncidentReason | None) -> IncidentReason | None:
    return first if severity(first) <= severity(second) else second


def freshness_limit(*, segment_seconds: int, index_interval_seconds: float) -> float:
    """How old the newest completed segment may be before recording is stale.

    Three delays stack up between a camera working and a row appearing in the
    index: the segment currently being written is not finished, the indexer
    sweeps on its own interval, and probing the finished file takes a moment.
    Adding them means a perfectly healthy camera is never flagged, at the cost
    of noticing a genuinely stalled one a little later than the recorder's own
    watchdog does.
    """
    stall = max(RECORDER_STALL_MIN_SECONDS, float(segment_seconds) * 2.0)
    return stall + max(index_interval_seconds, 0.0) + FRESHNESS_MARGIN_SECONDS


@dataclass(frozen=True, slots=True)
class CameraTelemetry:
    """One camera's inputs, all of them already known to some other subsystem."""

    camera_id: str
    camera_name: str
    enabled: bool
    recording_enabled: bool
    """The per-camera recording switch.

    Separate from ``enabled``, which also removes live view and ONVIF. A camera
    an administrator deliberately stopped recording is not a fault, and without
    this the monitor would see a configured camera with no recorder and no fresh
    segments and open an incident that could never close.
    """
    recording_mode: RecordingMode
    motion_enabled: bool

    privacy_enabled: bool
    storage_available: bool

    recorder_present: bool
    """Whether the recording manager is supervising a process for this camera."""
    recorder_state: ProcessState | None
    recorder_started_at: datetime | None
    restart_count: int
    consecutive_failures: int
    last_exit_code: int | None
    fault: StreamFault | None
    """Classified from the supervisor's last output; never the output itself."""
    source: RecorderSource | None

    last_segment_at: datetime | None
    """Start of the newest completed segment in the index, not a disk read."""

    motion_expected: bool
    motion_running: bool

    relay_expected: bool
    relay_ready: bool

    segment_seconds: int
    index_interval_seconds: float

    coverage_ratio: float | None = None
    """Recorded fraction of :data:`COVERAGE_WINDOW_SECONDS`.

    ``None`` when it has not been measured -- a camera too new to judge, or a
    caller that did not ask for it. It is never assumed to be good.
    """
    coverage_segments: int = 0
    coverage_window_seconds: float = COVERAGE_WINDOW_SECONDS
    coverage_gap_count: int = 0
    coverage_longest_gap_seconds: float = 0.0
    damaged_segments: int = 0
    short_segments: int = 0
    restarts_in_window: int = 0
    corruption_per_minute: float = 0.0


@dataclass(frozen=True, slots=True)
class Assessment:
    """What the rules concluded about one camera at one moment."""

    state: CameraState
    reason: IncidentReason | None
    """``None`` means nothing is wrong that should open an incident."""
    detail: str
    recording_expected: bool
    recording_fresh: bool | None
    """``None`` when recording is not expected, so the interface can say so
    instead of claiming a switched-off camera has stale footage."""
    segment_age_seconds: float | None
    fault: StreamFault | None
    source: RecorderSource | None
    coverage_ratio: float | None = None
    """Carried through so the interface can show the measurement behind the
    verdict rather than only the verdict."""


def _age(moment: datetime | None, now: datetime) -> float | None:
    return None if moment is None else max((now - moment).total_seconds(), 0.0)


def _idle(state: CameraState, detail: str, telemetry: CameraTelemetry, now: datetime) -> Assessment:
    """An assessment for a camera nothing is expected of."""
    return Assessment(
        state=state,
        reason=None,
        detail=detail,
        recording_expected=False,
        recording_fresh=None,
        segment_age_seconds=_age(telemetry.last_segment_at, now),
        fault=None,
        source=None,
    )


def assess(telemetry: CameraTelemetry, *, now: datetime, incident_open: bool = False) -> Assessment:
    """Decide one camera's state.

    ``incident_open`` is what turns "nothing is wrong at this instant" into
    ``recovering``. A camera whose incident has not been closed yet is not the
    same thing as one that has been fine all day, and saying so is the
    difference between a trustworthy status view and a flickering one.
    """
    if telemetry.privacy_enabled:
        # Checked before everything else. A deliberate, administrator-selected
        # pause must never read as an outage, and must never open an incident
        # or start a recovery loop.
        return _idle(
            CameraState.PRIVACY_PAUSED,
            "Paused by privacy mode. Recording and live view are stopped on purpose.",
            telemetry,
            now,
        )

    if not telemetry.enabled:
        return _idle(
            CameraState.DISABLED,
            "This camera is switched off in Sentinel.",
            telemetry,
            now,
        )

    if not telemetry.recording_enabled:
        # Deliberate, and reported as such. Reaching the freshness checks below
        # would open an incident for a camera doing exactly what it was told.
        return _idle(
            CameraState.DISABLED,
            "Recording is switched off for this camera.",
            telemetry,
            now,
        )

    if telemetry.recording_mode not in RECORDING_MODES:
        return _idle(
            CameraState.DISABLED,
            "Continuous recording is not selected for this camera.",
            telemetry,
            now,
        )

    limit = freshness_limit(
        segment_seconds=telemetry.segment_seconds,
        index_interval_seconds=telemetry.index_interval_seconds,
    )
    segment_age = _age(telemetry.last_segment_at, now)
    uptime = _age(telemetry.recorder_started_at, now)
    running = telemetry.recorder_state is ProcessState.RUNNING

    def verdict(
        state: CameraState,
        reason: IncidentReason | None,
        detail: str,
        *,
        fresh: bool | None = None,
    ) -> Assessment:
        recorder_fault = reason in {
            IncidentReason.RECORDING_NOT_FRESH,
            IncidentReason.RECORDER_RESTARTING,
            IncidentReason.STREAM_SOURCE_FAILURE,
            # Corruption is exactly the case where the classified label -- empty
            # RTP packets, missing parameter sets -- is the useful part.
            IncidentReason.STREAM_CORRUPTION,
        }
        return Assessment(
            state=state,
            reason=reason,
            detail=detail,
            recording_expected=True,
            recording_fresh=fresh,
            segment_age_seconds=segment_age,
            # A missing SPS/PPS sequence can leave FFmpeg alive but unable to
            # finish a segment. Keep the classified label for both a crashed
            # recorder and a live-but-stalled one; never carry it into healthy
            # or unrelated storage/relay/motion states.
            fault=telemetry.fault if recorder_fault else None,
            source=telemetry.source,
            coverage_ratio=telemetry.coverage_ratio,
        )

    if not telemetry.storage_available:
        return verdict(
            CameraState.OFFLINE,
            IncidentReason.STORAGE_UNAVAILABLE,
            "Recording is stopped because the recording volume is unavailable.",
            fresh=False,
        )

    if not telemetry.recorder_present:
        return verdict(
            CameraState.OFFLINE,
            IncidentReason.RECORDER_UNAVAILABLE,
            "Continuous recording is selected, but no recorder is running for this camera.",
            fresh=False,
        )

    if not running:
        stream_failing = telemetry.last_exit_code not in (None, 0)
        failure_reason = (
            IncidentReason.STREAM_SOURCE_FAILURE
            if stream_failing
            else IncidentReason.RECORDER_UNAVAILABLE
        )
        if telemetry.consecutive_failures >= OFFLINE_AFTER_FAILURES:
            return verdict(
                CameraState.OFFLINE,
                failure_reason,
                f"The recorder has failed {telemetry.consecutive_failures} times in a row "
                "and is still retrying.",
                fresh=False,
            )
        if telemetry.recorder_state in (ProcessState.STARTING, ProcessState.RESTARTING):
            return verdict(
                CameraState.RECOVERING,
                IncidentReason.RECORDER_RESTARTING,
                "The recorder is reconnecting to this camera.",
                fresh=False,
            )
        return verdict(
            CameraState.OFFLINE,
            failure_reason,
            "The recorder is not running.",
            fresh=False,
        )

    # The recorder is up. Whether it is producing footage is a different
    # question, and the one that matters: an FFmpeg that cannot read a camera's
    # H.264 setup data stays alive indefinitely without ever writing a segment,
    # which is the failure a process-liveness check cannot see.
    warming_up = uptime is not None and uptime < limit

    if segment_age is None:
        if warming_up:
            return verdict(
                CameraState.RECOVERING,
                None,
                "Recording has started; waiting for the first completed segment.",
                fresh=False,
            )
        return verdict(
            CameraState.OFFLINE,
            IncidentReason.RECORDING_NOT_FRESH,
            "The recorder is running but has never completed a recording segment.",
            fresh=False,
        )

    if segment_age > limit:
        if warming_up:
            return verdict(
                CameraState.RECOVERING,
                None,
                "Recording restarted; waiting for the first completed segment.",
                fresh=False,
            )
        return verdict(
            CameraState.OFFLINE,
            IncidentReason.RECORDING_NOT_FRESH,
            "The recorder is running but no segment has completed for "
            f"{round(segment_age / 60)} minutes.",
            fresh=False,
        )

    # A recent segment exists. That is necessary but nowhere near sufficient:
    # the reference installation had a camera recording 50.5% of the hour while
    # reporting healthy, because "a segment arrived three minutes ago" says
    # nothing about the fifty-nine minutes around it. Continuity is measured
    # before any supporting service is considered.
    coverage = telemetry.coverage_ratio
    measurable = coverage is not None and telemetry.coverage_segments >= MIN_COVERAGE_SEGMENTS

    if measurable and coverage is not None and coverage < MIN_COVERAGE_RATIO:
        critical = coverage < CRITICAL_COVERAGE_RATIO
        return verdict(
            CameraState.OFFLINE if critical else CameraState.DEGRADED,
            IncidentReason.RECORDING_COVERAGE_LOW,
            f"Only {coverage * 100:.0f}% of the last hour was recorded, in "
            f"{telemetry.coverage_gap_count} gaps "
            f"(longest {round(telemetry.coverage_longest_gap_seconds)}s).",
            fresh=True,
        )

    if telemetry.restarts_in_window >= RESTART_LOOP_THRESHOLD:
        return verdict(
            CameraState.DEGRADED,
            IncidentReason.RECORDING_UNSTABLE,
            f"The recorder restarted {telemetry.restarts_in_window} times in the last "
            "hour, so footage is repeatedly interrupted.",
            fresh=True,
        )

    if telemetry.corruption_per_minute >= CORRUPTION_RATE_THRESHOLD_PER_MINUTE:
        return verdict(
            CameraState.DEGRADED,
            IncidentReason.STREAM_CORRUPTION,
            f"The camera is delivering damaged media at about "
            f"{round(telemetry.corruption_per_minute)} events a minute.",
            fresh=True,
        )

    # Footage is arriving and it is continuous. Everything below here is a
    # supporting service.
    if telemetry.relay_expected and not telemetry.relay_ready:
        return verdict(
            CameraState.DEGRADED,
            IncidentReason.RELAY_UNAVAILABLE,
            "Recording is fresh, but the local stream relay is not answering, "
            "so live view may be unavailable.",
            fresh=True,
        )

    if telemetry.motion_expected and not telemetry.motion_running:
        return verdict(
            CameraState.DEGRADED,
            IncidentReason.MOTION_WORKER_UNAVAILABLE,
            "Recording is fresh, but motion detection is not running for this camera.",
            fresh=True,
        )

    if incident_open:
        return verdict(
            CameraState.RECOVERING,
            None,
            "Recording is fresh again. Waiting for the recovery to hold before "
            "closing the incident.",
            fresh=True,
        )

    return verdict(CameraState.HEALTHY, None, "Recording normally.", fresh=True)
