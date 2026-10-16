"""Response models for the Reliability Center.

Two audiences, two shapes, and the difference between them is the point.

An administrator gets the operational detail needed to act: supervised process
state, restart counts, which input is in use, the classified fault and the
incident history. A footage operator gets a narrow summary -- state, whether
recording is fresh, when the last segment landed and a fixed issue label.

Neither shape can carry a host, an RTSP URL, a filesystem path, a credential, a
process command line or captured camera output, because no field here is ever
populated from one. Reason and fault values come from closed vocabularies, and
``detail`` is a sentence Sentinel composed itself.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.database.models import (
    CameraIncident,
    IncidentReason,
    RecorderSource,
    RecordingProfile,
    StreamFault,
)
from app.recording.source_policy import REASON_LABELS as SOURCE_REASON_LABELS
from app.recording.source_policy import SourceReason
from app.reliability.monitor import CameraReport, RecoveryOutcome, ReliabilitySnapshot
from app.reliability.state import FAULT_LABELS, REASON_LABELS, CameraState
from app.supervisor import ProcessState


class IncidentResponse(BaseModel):
    id: str
    camera_id: str
    reason: IncidentReason
    reason_label: str
    started_at: datetime
    resolved_at: datetime | None
    duration_seconds: float | None
    active: bool
    detail: str | None
    fault: StreamFault | None
    fault_label: str | None
    source: RecorderSource | None
    restart_count: int


class RecorderDetail(BaseModel):
    """The recorder process, as far as an administrator needs to see it.

    No PID and no command line: neither helps from a browser, and both belong
    to the diagnostics bundle that already exists for that purpose.
    """

    present: bool
    state: ProcessState | None
    running: bool
    started_at: datetime | None
    restart_count: int
    consecutive_failures: int
    source: RecorderSource | None
    fault: StreamFault | None
    fault_label: str | None


class RecordingQuality(BaseModel):
    """Measured continuity for one camera over the trailing coverage window.

    This is the block that would have caught the reference installation's
    50.5%-coverage camera, which the previous freshness-only check reported as
    healthy because it had written a segment three minutes earlier.
    """

    window_seconds: float
    coverage_ratio: float | None
    """``None`` while there is not yet enough measured footage to judge."""
    coverage_percent: float | None
    recorded_seconds: float
    segments: int
    damaged_segments: int
    short_segments: int
    gap_count: int
    longest_gap_seconds: float
    restarts_in_window: int
    corruption_per_minute: float
    corruption_events: int
    corruption_kind: str | None
    """Classified category only -- never a fragment of recorder output."""
    profile: RecordingProfile | None
    """The concrete profile the running recorder was started with."""
    source_reason: SourceReason | None
    source_reason_label: str | None
    source_switches: int


class CameraReliabilityResponse(BaseModel):
    camera_id: str
    name: str
    state: CameraState
    detail: str
    issue: IncidentReason | None
    issue_label: str | None
    recording_expected: bool
    recording_fresh: bool | None
    last_segment_at: datetime | None
    segment_age_seconds: float | None
    freshness_limit_seconds: float
    last_healthy_at: datetime | None
    motion_expected: bool
    motion_running: bool
    recorder: RecorderDetail
    quality: RecordingQuality
    active_incident: IncidentResponse | None
    recent_incidents: list[IncidentResponse]


class ReliabilityResponse(BaseModel):
    generated_at: datetime
    privacy_enabled: bool
    privacy_message: str | None
    storage_available: bool
    relay_expected: bool
    relay_ready: bool
    counts: dict[CameraState, int]
    cameras: list[CameraReliabilityResponse]


class IncidentListResponse(BaseModel):
    incidents: list[IncidentResponse]
    total: int
    limit: int
    offset: int


class CameraRecoveryResponse(BaseModel):
    """The honest result of a manual retry.

    ``state_before`` is the state at the moment the request arrived, and there
    is deliberately no "state_after": restarting a recorder takes longer than
    an HTTP round trip, so any success claimed here would be invented.
    """

    camera_id: str
    requested_at: datetime
    accepted: bool
    actions: list[str]
    skipped: list[str]
    state_before: CameraState
    message: str


class SourceResetResponse(BaseModel):
    """The honest result of clearing a camera's learned recorder source.

    There is deliberately no claim that the relay now works: the decision is
    applied on the recorder's next start, and whether it qualifies is something
    only the next few minutes can establish.
    """

    camera_id: str
    reason: SourceReason
    reason_label: str
    message: str


class ViewerCameraReliability(BaseModel):
    """What a footage operator may see about one camera.

    Coverage is included because "this camera only recorded half the hour" is
    exactly what an operator needs before they trust the timeline. The
    *reasons* -- process state, restart counts, which input, the classified
    stream fault, the incident history -- remain administrator-only.
    """

    camera_id: str
    name: str
    state: CameraState
    recording_fresh: bool | None
    last_recording_at: datetime | None
    issue: IncidentReason | None
    issue_label: str | None
    coverage_percent: float | None
    """Rounded to whole per cent: an operator needs the magnitude, not a
    measurement precise enough to fingerprint a camera's behaviour."""


class ViewerReliabilityResponse(BaseModel):
    generated_at: datetime
    privacy_enabled: bool
    privacy_message: str | None
    healthy: int
    needs_attention: int
    cameras: list[ViewerCameraReliability]


#: States that mean an operator should look at something.
ATTENTION_STATES = frozenset({CameraState.OFFLINE, CameraState.DEGRADED, CameraState.RECOVERING})


def incident_response(incident: CameraIncident) -> IncidentResponse:
    return IncidentResponse(
        id=incident.id,
        camera_id=incident.camera_id,
        reason=incident.reason,
        reason_label=REASON_LABELS.get(incident.reason, "Camera fault"),
        started_at=incident.started_at,
        resolved_at=incident.resolved_at,
        duration_seconds=incident.duration_seconds,
        active=incident.resolved_at is None,
        detail=incident.detail,
        fault=incident.fault,
        fault_label=FAULT_LABELS.get(incident.fault) if incident.fault else None,
        source=incident.source,
        restart_count=incident.restart_count,
    )


def camera_response(report: CameraReport) -> CameraReliabilityResponse:
    assessment = report.assessment
    telemetry = report.telemetry
    # The supervisor keeps its last output line after recovery. Exposing that
    # raw classification here would make a healthy camera display a stale
    # fault indefinitely, so only the currently relevant assessment carries it.
    fault = assessment.fault
    return CameraReliabilityResponse(
        camera_id=report.camera_id,
        name=report.camera_name,
        state=assessment.state,
        detail=assessment.detail,
        issue=assessment.reason,
        issue_label=REASON_LABELS.get(assessment.reason) if assessment.reason else None,
        recording_expected=assessment.recording_expected,
        recording_fresh=assessment.recording_fresh,
        last_segment_at=telemetry.last_segment_at,
        segment_age_seconds=assessment.segment_age_seconds,
        freshness_limit_seconds=report.freshness_limit_seconds,
        last_healthy_at=report.last_healthy_at,
        motion_expected=telemetry.motion_expected,
        motion_running=telemetry.motion_running,
        quality=RecordingQuality(
            window_seconds=telemetry.coverage_window_seconds,
            coverage_ratio=telemetry.coverage_ratio,
            coverage_percent=(
                telemetry.coverage_ratio * 100.0 if telemetry.coverage_ratio is not None else None
            ),
            recorded_seconds=(
                telemetry.coverage_ratio * telemetry.coverage_window_seconds
                if telemetry.coverage_ratio is not None
                else 0.0
            ),
            segments=telemetry.coverage_segments,
            damaged_segments=telemetry.damaged_segments,
            short_segments=telemetry.short_segments,
            gap_count=telemetry.coverage_gap_count,
            longest_gap_seconds=telemetry.coverage_longest_gap_seconds,
            restarts_in_window=telemetry.restarts_in_window,
            corruption_per_minute=round(telemetry.corruption_per_minute, 2),
            corruption_events=report.corruption_events,
            corruption_kind=report.corruption_kind,
            profile=report.profile,
            source_reason=report.source_reason,
            source_reason_label=(
                SOURCE_REASON_LABELS.get(report.source_reason) if report.source_reason else None
            ),
            source_switches=report.source_switches,
        ),
        recorder=RecorderDetail(
            present=telemetry.recorder_present,
            state=telemetry.recorder_state,
            running=telemetry.recorder_state is ProcessState.RUNNING,
            started_at=telemetry.recorder_started_at,
            restart_count=telemetry.restart_count,
            consecutive_failures=telemetry.consecutive_failures,
            source=telemetry.source,
            fault=fault,
            fault_label=FAULT_LABELS.get(fault) if fault else None,
        ),
        active_incident=(
            incident_response(report.active_incident)
            if report.active_incident is not None
            else None
        ),
        recent_incidents=[incident_response(item) for item in report.recent_incidents],
    )


def snapshot_response(snapshot: ReliabilitySnapshot) -> ReliabilityResponse:
    return ReliabilityResponse(
        generated_at=snapshot.generated_at,
        privacy_enabled=snapshot.privacy_enabled,
        privacy_message=snapshot.privacy_message,
        storage_available=snapshot.storage_available,
        relay_expected=snapshot.relay_expected,
        relay_ready=snapshot.relay_ready,
        counts={state: snapshot.count(state) for state in CameraState},
        cameras=[camera_response(report) for report in snapshot.cameras],
    )


def viewer_response(snapshot: ReliabilitySnapshot) -> ViewerReliabilityResponse:
    """The narrow operator view.

    Built field by field from the report rather than by trimming the
    administrator model, so a field added there cannot leak into this one by
    default.
    """
    visible = [report for report in snapshot.cameras if report.telemetry.enabled]
    cameras = [
        ViewerCameraReliability(
            camera_id=report.camera_id,
            name=report.camera_name,
            state=report.assessment.state,
            recording_fresh=report.assessment.recording_fresh,
            last_recording_at=report.telemetry.last_segment_at,
            issue=report.assessment.reason,
            issue_label=(
                REASON_LABELS.get(report.assessment.reason) if report.assessment.reason else None
            ),
            coverage_percent=(
                round(report.telemetry.coverage_ratio * 100.0)
                if report.telemetry.coverage_ratio is not None
                else None
            ),
        )
        for report in visible
    ]
    return ViewerReliabilityResponse(
        generated_at=snapshot.generated_at,
        privacy_enabled=snapshot.privacy_enabled,
        privacy_message=snapshot.privacy_message,
        healthy=sum(1 for report in visible if report.assessment.state is CameraState.HEALTHY),
        needs_attention=sum(1 for report in visible if report.assessment.state in ATTENTION_STATES),
        cameras=cameras,
    )


def recovery_response(outcome: RecoveryOutcome, *, message: str) -> CameraRecoveryResponse:
    return CameraRecoveryResponse(
        camera_id=outcome.camera_id,
        requested_at=outcome.requested_at,
        accepted=bool(outcome.actions),
        actions=list(outcome.actions),
        skipped=list(outcome.skipped),
        state_before=outcome.state_before,
        message=message,
    )
