"""The Camera Reliability Center: state rules, incidents, privacy and access."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.config import Settings
from app.database.models import (
    CameraIncident,
    IncidentReason,
    RecorderSource,
    RecordingMode,
    RecordingSegment,
    SegmentStatus,
    StreamFault,
)
from app.database.session import session_scope
from app.motion.manager import get_manager as get_motion_manager
from app.recording.manager import RecordingManager
from app.recording.manager import get_manager as get_recording_manager
from app.recording.source_policy import CameraSourceState, SourceReason
from app.reliability import service
from app.reliability.faults import classify
from app.reliability.monitor import (
    OPEN_AFTER_SECONDS,
    RECOVER_AFTER_SECONDS,
    RecoveryThrottledError,
    get_monitor,
)
from app.reliability.state import (
    CameraState,
    CameraTelemetry,
    assess,
    freshness_limit,
    worst,
)
from app.streaming.manager import get_manager as get_streaming_manager
from app.supervisor import ProcessState
from app.system import settings_store
from app.viewer.auth import VIEWER_KEY_HEADER

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)

CAMERA_PAYLOAD = {
    "name": "Courtyard",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
    "sub_stream_url": "rtsp://192.168.50.50:554/stream2",
    "username": "camera-admin",
    "password": "camera-secret",
}

#: With a 60 s segment and the default 30 s index sweep this is 210 s.
LIMIT = freshness_limit(segment_seconds=60, index_interval_seconds=30.0)


def telemetry(**overrides: object) -> CameraTelemetry:
    """A healthy camera, with individual facts overridden per test."""
    base: dict[str, object] = {
        "camera_id": "cam-1",
        "camera_name": "Courtyard",
        "enabled": True,
        "recording_enabled": True,
        "recording_mode": RecordingMode.CONTINUOUS,
        "motion_enabled": True,
        "privacy_enabled": False,
        "storage_available": True,
        "recorder_present": True,
        "recorder_state": ProcessState.RUNNING,
        "recorder_started_at": NOW - timedelta(hours=6),
        "restart_count": 0,
        "consecutive_failures": 0,
        "last_exit_code": None,
        "fault": None,
        "source": RecorderSource.RELAY,
        "last_segment_at": NOW - timedelta(seconds=45),
        "motion_expected": True,
        "motion_running": True,
        "relay_expected": True,
        "relay_ready": True,
        "segment_seconds": 60,
        "index_interval_seconds": 30.0,
    }
    base.update(overrides)
    return CameraTelemetry(**base)  # type: ignore[arg-type]


class TestDeliberatelyStoppedRecording:
    """A camera an administrator switched off is not a camera that broke."""

    def test_it_reads_as_disabled_rather_than_as_a_fault(self) -> None:
        result = assess(
            telemetry(
                recording_enabled=False,
                recorder_present=False,
                recorder_state=None,
                recorder_started_at=None,
                # Long past any freshness limit: without the switch being
                # visible here, this is exactly what opens an incident.
                last_segment_at=NOW - timedelta(hours=9),
            ),
            now=NOW,
        )
        assert result.state is CameraState.DISABLED
        assert result.reason is None, "a deliberate pause must not open an incident"
        assert result.recording_expected is False
        assert "switched off" in result.detail

    def test_it_stays_disabled_even_with_an_incident_already_open(self) -> None:
        result = assess(
            telemetry(recording_enabled=False, recorder_present=False, recorder_state=None),
            now=NOW,
            incident_open=True,
        )
        assert result.state is CameraState.DISABLED
        assert result.reason is None

    def test_turning_it_back_on_restores_the_ordinary_assessment(self) -> None:
        stale = telemetry(
            recording_enabled=True,
            recorder_present=False,
            recorder_state=None,
            recorder_started_at=None,
            last_segment_at=NOW - timedelta(hours=9),
        )
        result = assess(stale, now=NOW)
        assert result.state is not CameraState.DISABLED
        assert result.reason is not None, "a camera that should be recording and is not is a fault"


class TestOperationalState:
    def test_a_recording_camera_with_recent_footage_is_healthy(self) -> None:
        result = assess(telemetry(), now=NOW)
        assert result.state is CameraState.HEALTHY
        assert result.reason is None
        assert result.recording_fresh is True
        assert result.recording_expected is True

    def test_privacy_mode_outranks_every_other_signal(self) -> None:
        """A deliberate pause must never be reported as an outage."""
        result = assess(
            telemetry(
                privacy_enabled=True,
                recorder_present=False,
                storage_available=False,
                consecutive_failures=9,
                last_segment_at=NOW - timedelta(days=2),
            ),
            now=NOW,
            incident_open=True,
        )
        assert result.state is CameraState.PRIVACY_PAUSED
        assert result.reason is None
        assert result.recording_expected is False
        assert result.recording_fresh is None

    def test_a_switched_off_camera_is_disabled_not_offline(self) -> None:
        result = assess(
            telemetry(enabled=False, recorder_present=False, last_segment_at=None), now=NOW
        )
        assert result.state is CameraState.DISABLED
        assert result.reason is None

    def test_recording_switched_off_is_disabled_not_a_fault(self) -> None:
        result = assess(
            telemetry(recording_mode=RecordingMode.OFF, recorder_present=False),
            now=NOW,
        )
        assert result.state is CameraState.DISABLED
        assert result.reason is None
        assert result.recording_expected is False

    @pytest.mark.parametrize("mode", [RecordingMode.MOTION, RecordingMode.SCHEDULED])
    def test_unimplemented_recording_modes_do_not_report_a_fault(self, mode: RecordingMode) -> None:
        result = assess(telemetry(recording_mode=mode, recorder_present=False), now=NOW)
        assert result.state is CameraState.DISABLED
        assert result.reason is None

    def test_missing_storage_stops_recording_and_says_so(self) -> None:
        result = assess(telemetry(storage_available=False), now=NOW)
        assert result.state is CameraState.OFFLINE
        assert result.reason is IncidentReason.STORAGE_UNAVAILABLE

    def test_a_camera_with_no_recorder_is_offline(self) -> None:
        result = assess(telemetry(recorder_present=False, recorder_state=None), now=NOW)
        assert result.state is CameraState.OFFLINE
        assert result.reason is IncidentReason.RECORDER_UNAVAILABLE

    def test_a_first_reconnect_is_recovering_rather_than_offline(self) -> None:
        result = assess(
            telemetry(
                recorder_state=ProcessState.RESTARTING,
                consecutive_failures=1,
                last_exit_code=1,
            ),
            now=NOW,
        )
        assert result.state is CameraState.RECOVERING
        assert result.reason is IncidentReason.RECORDER_RESTARTING

    def test_repeated_failures_stop_being_called_recovery(self) -> None:
        """A camera that has been unplugged is offline, not perpetually recovering."""
        result = assess(
            telemetry(
                recorder_state=ProcessState.RESTARTING,
                consecutive_failures=4,
                last_exit_code=1,
                fault=StreamFault.CONNECTION_FAILED,
            ),
            now=NOW,
        )
        assert result.state is CameraState.OFFLINE
        assert result.reason is IncidentReason.STREAM_SOURCE_FAILURE
        assert result.fault is StreamFault.CONNECTION_FAILED

    def test_a_live_process_that_writes_nothing_is_offline(self) -> None:
        """The failure a process-liveness check cannot see."""
        result = assess(
            telemetry(
                last_segment_at=NOW - timedelta(seconds=LIMIT + 120),
                fault=StreamFault.MISSING_STREAM_PARAMETERS,
            ),
            now=NOW,
        )
        assert result.state is CameraState.OFFLINE
        assert result.reason is IncidentReason.RECORDING_NOT_FRESH
        assert result.recording_fresh is False
        assert result.fault is StreamFault.MISSING_STREAM_PARAMETERS
        assert "minutes" in result.detail

    def test_a_freshly_started_recorder_is_given_time_to_write(self) -> None:
        result = assess(
            telemetry(
                recorder_started_at=NOW - timedelta(seconds=10),
                last_segment_at=None,
            ),
            now=NOW,
        )
        assert result.state is CameraState.RECOVERING
        assert result.reason is None

    def test_a_recorder_that_never_writes_eventually_counts_as_offline(self) -> None:
        result = assess(
            telemetry(recorder_started_at=NOW - timedelta(hours=1), last_segment_at=None),
            now=NOW,
        )
        assert result.state is CameraState.OFFLINE
        assert result.reason is IncidentReason.RECORDING_NOT_FRESH

    def test_an_unavailable_relay_degrades_a_recording_camera(self) -> None:
        result = assess(telemetry(relay_ready=False), now=NOW)
        assert result.state is CameraState.DEGRADED
        assert result.reason is IncidentReason.RELAY_UNAVAILABLE
        assert result.recording_fresh is True

    def test_a_stopped_motion_worker_degrades_but_does_not_hide_recording(self) -> None:
        result = assess(telemetry(motion_running=False), now=NOW)
        assert result.state is CameraState.DEGRADED
        assert result.reason is IncidentReason.MOTION_WORKER_UNAVAILABLE
        assert result.recording_fresh is True

    def test_motion_is_not_expected_when_the_camera_has_it_switched_off(self) -> None:
        result = assess(telemetry(motion_expected=False, motion_running=False), now=NOW)
        assert result.state is CameraState.HEALTHY

    def test_a_healthy_camera_with_an_open_incident_is_recovering(self) -> None:
        result = assess(telemetry(), now=NOW, incident_open=True)
        assert result.state is CameraState.RECOVERING
        assert result.reason is None
        assert result.recording_fresh is True

    def test_the_recovered_input_source_is_reported(self) -> None:
        result = assess(telemetry(source=RecorderSource.DIRECT), now=NOW)
        assert result.source is RecorderSource.DIRECT
        assert result.state is CameraState.HEALTHY


class TestFreshness:
    def test_the_limit_allows_for_the_segment_and_the_index_sweep(self) -> None:
        assert freshness_limit(segment_seconds=60, index_interval_seconds=30.0) == 210.0

    def test_a_long_segment_length_widens_the_window(self) -> None:
        assert freshness_limit(segment_seconds=300, index_interval_seconds=30.0) == 690.0

    def test_a_short_segment_length_keeps_the_floor(self) -> None:
        """Development installs use tiny segments; they must not thrash."""
        assert freshness_limit(segment_seconds=5, index_interval_seconds=0.0) == 180.0

    def test_footage_exactly_at_the_limit_still_counts_as_fresh(self) -> None:
        result = assess(telemetry(last_segment_at=NOW - timedelta(seconds=LIMIT)), now=NOW)
        assert result.state is CameraState.HEALTHY
        assert result.segment_age_seconds == LIMIT

    def test_one_second_past_the_limit_is_not_fresh(self) -> None:
        result = assess(telemetry(last_segment_at=NOW - timedelta(seconds=LIMIT + 1)), now=NOW)
        assert result.recording_fresh is False


class TestSeverity:
    def test_the_worse_reason_wins(self) -> None:
        assert (
            worst(IncidentReason.RECORDER_RESTARTING, IncidentReason.STORAGE_UNAVAILABLE)
            is IncidentReason.STORAGE_UNAVAILABLE
        )
        assert (
            worst(IncidentReason.RECORDING_NOT_FRESH, IncidentReason.MOTION_WORKER_UNAVAILABLE)
            is IncidentReason.RECORDING_NOT_FRESH
        )

    def test_a_real_reason_beats_nothing(self) -> None:
        assert worst(None, IncidentReason.RELAY_UNAVAILABLE) is IncidentReason.RELAY_UNAVAILABLE
        assert worst(None, None) is None


class TestFaultClassification:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("non-existing PPS 0 referenced", StreamFault.MISSING_STREAM_PARAMETERS),
            ("Could not find codec parameters for stream 0", StreamFault.MISSING_STREAM_PARAMETERS),
            ("dimensions not set", StreamFault.MISSING_STREAM_PARAMETERS),
            ("RTP: bad sequence number 4711", StreamFault.CORRUPT_PACKETS),
            ("RTP: bad cseq 4711 expected 4710", StreamFault.CORRUPT_PACKETS),
            ("Connection refused", StreamFault.CONNECTION_FAILED),
            ("Connection timed out", StreamFault.TIMEOUT),
            ("method DESCRIBE failed: 401 Unauthorized", StreamFault.AUTHENTICATION_REJECTED),
            ("something nobody has seen before", StreamFault.UNKNOWN),
        ],
    )
    def test_known_shapes_get_a_stable_label(self, message: str, expected: StreamFault) -> None:
        assert classify(message) is expected

    def test_nothing_to_classify_is_not_a_fault(self) -> None:
        assert classify(None) is None
        assert classify("") is None
        assert classify("   ") is None

    def test_the_original_text_never_escapes_the_classifier(self) -> None:
        """The whole reason this returns an enum and not a string."""
        message = "rtsp://admin:hunter2@192.168.50.50:554/stream1: Connection refused"
        result = classify(message)
        assert result is StreamFault.CONNECTION_FAILED
        assert "hunter2" not in result.value
        assert "192.168.50.50" not in result.value


class TestSanitisation:
    def test_a_credential_in_a_detail_is_redacted(self) -> None:
        cleaned = service.sanitise_detail(
            "Recorder failed on rtsp://admin:hunter2@192.168.50.50:554/stream1"
        )
        assert cleaned is not None
        assert "hunter2" not in cleaned
        assert "***" in cleaned

    def test_a_detail_is_bounded_to_the_column_width(self) -> None:
        cleaned = service.sanitise_detail("ffmpeg noise " * 200)
        assert cleaned is not None
        assert len(cleaned) <= service.MAX_DETAIL_CHARS

    def test_empty_details_stay_empty(self) -> None:
        assert service.sanitise_detail(None) is None
        assert service.sanitise_detail("") is None

    async def test_a_stored_incident_never_keeps_the_password(
        self, admin_client: AsyncClient
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            incident = await service.open_incident(
                session,
                camera["id"],
                reason=IncidentReason.STREAM_SOURCE_FAILURE,
                detail="Failed reading rtsp://admin:camera-secret@192.168.50.50:554/stream1",
                fault=StreamFault.CONNECTION_FAILED,
                source=RecorderSource.DIRECT,
                started_at=NOW,
            )
            stored = incident.detail or ""
        assert "camera-secret" not in stored
        assert "***" in stored


class TestIncidentRetention:
    async def test_resolved_incidents_age_out_and_open_ones_do_not(
        self, admin_client: AsyncClient
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        old = NOW - timedelta(days=service.RETENTION_DAYS + 2)
        async with session_scope() as session:
            session.add_all(
                [
                    CameraIncident(
                        camera_id=camera["id"],
                        reason=IncidentReason.RECORDING_NOT_FRESH,
                        started_at=old,
                        resolved_at=old + timedelta(minutes=5),
                        duration_seconds=300.0,
                    ),
                    CameraIncident(
                        camera_id=camera["id"],
                        reason=IncidentReason.RECORDER_UNAVAILABLE,
                        started_at=old,
                    ),
                ]
            )
            await session.flush()

        async with session_scope() as session:
            removed = await service.prune(session, now=NOW)
        assert removed == 1

        async with session_scope() as session:
            rows = (await session.execute(select(CameraIncident))).scalars().all()
        assert len(rows) == 1
        assert rows[0].resolved_at is None

    async def test_a_flapping_camera_cannot_grow_the_table_without_limit(
        self, admin_client: AsyncClient
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            for index in range(service.MAX_PER_CAMERA + 25):
                started = NOW - timedelta(minutes=index)
                session.add(
                    CameraIncident(
                        camera_id=camera["id"],
                        reason=IncidentReason.RECORDER_RESTARTING,
                        started_at=started,
                        resolved_at=started + timedelta(seconds=30),
                        duration_seconds=30.0,
                    )
                )
            await session.flush()

        async with session_scope() as session:
            removed = await service.prune(session, now=NOW)
        assert removed == 25

        async with session_scope() as session:
            total = len((await session.execute(select(CameraIncident))).scalars().all())
        assert total == service.MAX_PER_CAMERA

    async def test_retention_is_measured_from_recovery_not_incident_start(
        self, admin_client: AsyncClient
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            session.add(
                CameraIncident(
                    camera_id=camera["id"],
                    reason=IncidentReason.RECORDING_NOT_FRESH,
                    started_at=NOW - timedelta(days=service.RETENTION_DAYS + 5),
                    resolved_at=NOW - timedelta(days=1),
                    duration_seconds=4 * 24 * 60 * 60,
                )
            )
            await session.flush()

        async with session_scope() as session:
            removed = await service.prune(session, now=NOW)
        assert removed == 0
        assert len(await _incidents(camera["id"])) == 1


@pytest.fixture
def runtime(app: object) -> Iterator[Runtime]:
    """Stand in for the recorder, motion and relay telemetry the monitor reads."""
    del app  # the managers only exist once the lifespan has run
    helper = Runtime()
    try:
        yield helper
    finally:
        helper.restore()


class Runtime:
    """Drives manager telemetry without spawning FFmpeg or contacting a camera."""

    def __init__(self) -> None:
        self._recorders: dict[str, dict[str, object]] = {}
        self._workers: dict[str, dict[str, object]] = {}
        recording = get_recording_manager()
        motion = get_motion_manager()
        self._original_recorder_status = recording.recorder_status
        self._original_worker_status = motion.worker_status
        recording.recorder_status = lambda: list(self._recorders.values())  # type: ignore[method-assign]
        motion.worker_status = lambda: list(self._workers.values())  # type: ignore[method-assign]
        self.relay(ready=True)

    def restore(self) -> None:
        get_recording_manager().recorder_status = self._original_recorder_status  # type: ignore[method-assign]
        get_motion_manager().worker_status = self._original_worker_status  # type: ignore[method-assign]

    def recorder(self, camera_id: str, **fields: object) -> None:
        status: dict[str, object] = {
            "camera_id": camera_id,
            "name": f"recorder:{camera_id}",
            "state": ProcessState.RUNNING.value,
            "source": RecorderSource.RELAY.value,
            "restart_count": 0,
            "consecutive_failures": 0,
            "last_exit_code": None,
            "last_uptime_seconds": None,
            "last_error": None,
            "started_at": NOW - timedelta(hours=6),
        }
        status.update(fields)
        self._recorders[camera_id] = status

    def no_recorder(self, camera_id: str) -> None:
        self._recorders.pop(camera_id, None)

    def motion_worker(self, camera_id: str, *, running: bool = True) -> None:
        self._workers[camera_id] = {"camera_id": camera_id, "running": running}

    def relay(self, *, ready: bool) -> None:
        streaming = get_streaming_manager()
        if ready:
            streaming._ready.set()
        else:
            streaming._ready.clear()


async def _segment(camera_id: str, started_at: datetime) -> None:
    async with session_scope() as session:
        session.add(
            RecordingSegment(
                camera_id=camera_id,
                path=f"{camera_id}/{uuid.uuid4()}.mp4",
                started_at=started_at,
                ended_at=started_at + timedelta(seconds=60),
                duration_seconds=60.0,
                size_bytes=6_000_000,
                status=SegmentStatus.COMPLETE,
            )
        )
        await session.flush()


async def _incidents(camera_id: str) -> list[CameraIncident]:
    async with session_scope() as session:
        rows = await session.execute(
            select(CameraIncident)
            .where(CameraIncident.camera_id == camera_id)
            .order_by(CameraIncident.started_at)
        )
        return list(rows.scalars())


class TestIncidentLifecycle:
    async def test_a_brief_fault_is_debounced_and_a_sustained_one_is_not(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.no_recorder(camera_id)
        monitor = get_monitor()

        snapshot = await monitor.evaluate(now=NOW)
        assert snapshot.cameras[0].assessment.state is CameraState.OFFLINE
        assert await _incidents(camera_id) == []

        await monitor.evaluate(now=NOW + timedelta(seconds=OPEN_AFTER_SECONDS - 1))
        assert await _incidents(camera_id) == []

        await monitor.evaluate(now=NOW + timedelta(seconds=OPEN_AFTER_SECONDS))
        opened = await _incidents(camera_id)
        assert len(opened) == 1
        assert opened[0].reason is IncidentReason.RECORDER_UNAVAILABLE
        # Dated from when the fault began, not from when the debounce expired.
        assert opened[0].started_at == NOW
        assert opened[0].resolved_at is None

    async def test_a_fault_that_clears_before_the_window_leaves_no_trace(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        monitor = get_monitor()

        runtime.no_recorder(camera_id)
        await monitor.evaluate(now=NOW)
        await monitor.evaluate(now=NOW + timedelta(seconds=30))

        runtime.recorder(camera_id)
        runtime.motion_worker(camera_id)
        await _segment(camera_id, NOW + timedelta(seconds=30))
        await monitor.evaluate(now=NOW + timedelta(seconds=60))

        assert await _incidents(camera_id) == []

    async def test_recovery_must_hold_before_an_incident_closes(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        monitor = get_monitor()

        runtime.no_recorder(camera_id)
        await monitor.evaluate(now=NOW)
        await monitor.evaluate(now=NOW + timedelta(seconds=OPEN_AFTER_SECONDS))
        assert len(await _incidents(camera_id)) == 1

        recovered_at = NOW + timedelta(seconds=300)
        runtime.recorder(camera_id, started_at=recovered_at - timedelta(hours=1))
        runtime.motion_worker(camera_id)
        await _segment(camera_id, recovered_at - timedelta(seconds=30))

        snapshot = await monitor.evaluate(now=recovered_at)
        report = snapshot.cameras[0]
        assert report.assessment.state is CameraState.RECOVERING
        assert report.assessment.recording_fresh is True
        assert report.active_incident is not None

        still_open = NOW + timedelta(seconds=300 + RECOVER_AFTER_SECONDS - 1)
        await _segment(camera_id, still_open - timedelta(seconds=30))
        snapshot = await monitor.evaluate(now=still_open)
        assert snapshot.cameras[0].assessment.state is CameraState.RECOVERING

        closed_at = NOW + timedelta(seconds=300 + RECOVER_AFTER_SECONDS)
        await _segment(camera_id, closed_at - timedelta(seconds=30))
        snapshot = await monitor.evaluate(now=closed_at)
        report = snapshot.cameras[0]
        assert report.assessment.state is CameraState.HEALTHY
        assert report.active_incident is None

        incidents = await _incidents(camera_id)
        assert len(incidents) == 1
        assert incidents[0].resolved_at == closed_at
        assert incidents[0].duration_seconds == (closed_at - NOW).total_seconds()

    async def test_one_outage_stays_one_incident_and_keeps_the_worst_reason(
        self, admin_client: AsyncClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A camera cycling through symptoms is one outage, not a list of them."""
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        monitor = get_monitor()

        # Fresh footage, but the relay is down: the mildest of the reasons.
        runtime.recorder(camera_id)
        runtime.motion_worker(camera_id)
        runtime.relay(ready=False)
        await _segment(camera_id, NOW - timedelta(seconds=30))
        await monitor.evaluate(now=NOW)
        await monitor.evaluate(now=NOW + timedelta(seconds=OPEN_AFTER_SECONDS))

        incidents = await _incidents(camera_id)
        assert len(incidents) == 1
        assert incidents[0].reason is IncidentReason.RELAY_UNAVAILABLE

        # Storage then disappears, which is far worse.
        monkeypatch.setattr(
            RecordingManager,
            "suspended_reason",
            property(lambda self: "The recording volume is not mounted."),
        )
        await monitor.evaluate(now=NOW + timedelta(seconds=200))

        incidents = await _incidents(camera_id)
        assert len(incidents) == 1
        assert incidents[0].reason is IncidentReason.STORAGE_UNAVAILABLE
        assert incidents[0].resolved_at is None

    async def test_a_disabled_camera_never_opens_an_incident(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        await admin_client.patch(f"/api/v1/cameras/{camera_id}", json={"enabled": False})
        runtime.no_recorder(camera_id)
        monitor = get_monitor()

        for offset in (0, 90, 400, 900):
            snapshot = await monitor.evaluate(now=NOW + timedelta(seconds=offset))
        assert snapshot.cameras[0].assessment.state is CameraState.DISABLED
        assert await _incidents(camera_id) == []

    async def test_a_camera_with_recording_switched_off_never_opens_an_incident(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        await admin_client.patch(f"/api/v1/cameras/{camera_id}", json={"recording_mode": "off"})
        runtime.no_recorder(camera_id)
        monitor = get_monitor()

        for offset in (0, 90, 400, 900):
            snapshot = await monitor.evaluate(now=NOW + timedelta(seconds=offset))
        assert snapshot.cameras[0].assessment.state is CameraState.DISABLED
        assert await _incidents(camera_id) == []

    async def test_the_classified_fault_reaches_the_incident_but_the_output_does_not(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(
            camera_id,
            state=ProcessState.RESTARTING.value,
            consecutive_failures=5,
            last_exit_code=1,
            source=RecorderSource.DIRECT.value,
            last_error=(
                "rtsp://admin:camera-secret@192.168.50.50:554/stream1: "
                "non-existing PPS 0 referenced"
            ),
        )
        monitor = get_monitor()
        await monitor.evaluate(now=NOW)
        await monitor.evaluate(now=NOW + timedelta(seconds=OPEN_AFTER_SECONDS))

        incidents = await _incidents(camera_id)
        assert len(incidents) == 1
        assert incidents[0].reason is IncidentReason.STREAM_SOURCE_FAILURE
        assert incidents[0].fault is StreamFault.MISSING_STREAM_PARAMETERS
        assert incidents[0].source is RecorderSource.DIRECT
        assert "camera-secret" not in (incidents[0].detail or "")
        assert "192.168.50.50" not in (incidents[0].detail or "")

    async def test_incident_restart_count_is_observed_during_the_outage(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        monitor = get_monitor()

        # Establish the supervisor's existing lifetime counter while healthy;
        # those historical four restarts do not belong to the new incident.
        runtime.recorder(camera_id, restart_count=4)
        runtime.motion_worker(camera_id)
        await _segment(camera_id, NOW - timedelta(seconds=30))
        await monitor.evaluate(now=NOW)

        runtime.recorder(
            camera_id,
            state=ProcessState.RESTARTING.value,
            restart_count=5,
            consecutive_failures=1,
            last_exit_code=1,
        )
        await monitor.evaluate(now=NOW + timedelta(seconds=30))
        runtime.recorder(
            camera_id,
            state=ProcessState.RESTARTING.value,
            restart_count=7,
            consecutive_failures=3,
            last_exit_code=1,
        )
        await monitor.evaluate(now=NOW + timedelta(seconds=30 + OPEN_AFTER_SECONDS))

        incidents = await _incidents(camera_id)
        assert incidents[0].restart_count == 3

        # Rebuilding a process resets its lifetime counter. The reset itself is
        # not a restart, but subsequent observed increments still count.
        runtime.recorder(
            camera_id,
            state=ProcessState.RESTARTING.value,
            restart_count=0,
            consecutive_failures=1,
            last_exit_code=1,
        )
        await monitor.evaluate(now=NOW + timedelta(seconds=180))
        runtime.recorder(
            camera_id,
            state=ProcessState.RESTARTING.value,
            restart_count=1,
            consecutive_failures=2,
            last_exit_code=1,
        )
        await monitor.evaluate(now=NOW + timedelta(seconds=210))
        assert (await _incidents(camera_id))[0].restart_count == 4


class TestPrivacyMode:
    async def test_privacy_reports_paused_cameras_and_opens_no_incident(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.no_recorder(camera_id)

        enabled = await admin_client.put(
            "/api/v1/system/privacy", json={"enabled": True, "reason": "guests"}
        )
        assert enabled.status_code == 200, enabled.text

        monitor = get_monitor()
        for offset in (0, 90, 300, 900):
            snapshot = await monitor.evaluate(now=NOW + timedelta(seconds=offset))

        assert snapshot.privacy_enabled is True
        assert snapshot.relay_expected is False
        assert snapshot.cameras[0].assessment.state is CameraState.PRIVACY_PAUSED
        assert await _incidents(camera_id) == []

    async def test_privacy_freezes_an_open_incident_instead_of_faking_a_recovery(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.no_recorder(camera_id)
        monitor = get_monitor()

        await monitor.evaluate(now=NOW)
        await monitor.evaluate(now=NOW + timedelta(seconds=OPEN_AFTER_SECONDS))
        assert len(await _incidents(camera_id)) == 1

        await admin_client.put(
            "/api/v1/system/privacy", json={"enabled": True, "reason": "maintenance"}
        )
        for offset in (200, 600, 1200):
            await monitor.evaluate(now=NOW + timedelta(seconds=offset))

        incidents = await _incidents(camera_id)
        assert len(incidents) == 1
        assert incidents[0].resolved_at is None

    async def test_manual_recovery_is_refused_while_privacy_is_active(
        self, admin_client: AsyncClient
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await admin_client.put(
            "/api/v1/system/privacy", json={"enabled": True, "reason": "private_event"}
        )
        response = await admin_client.post(f"/api/v1/reliability/cameras/{camera['id']}/retry")
        assert response.status_code == 423
        assert "Private event in progress" in response.json()["detail"]

    async def test_the_monitor_itself_refuses_a_retry_during_privacy(
        self, admin_client: AsyncClient
    ) -> None:
        """Defence in depth: the manager refuses even without the HTTP guard."""
        from fastapi import HTTPException

        from app.database.models import Camera

        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await admin_client.put("/api/v1/system/privacy", json={"enabled": True, "reason": "guests"})
        async with session_scope() as session:
            record = await session.get(Camera, camera["id"])
            assert record is not None
            with pytest.raises(HTTPException) as raised:
                await get_monitor().retry_camera(record)
        assert raised.value.status_code == 423


class TestReliabilityApi:
    async def test_every_administrator_route_requires_authentication(
        self, client: AsyncClient
    ) -> None:
        assert (await client.get("/api/v1/reliability/cameras")).status_code == 401
        assert (await client.get("/api/v1/reliability/incidents")).status_code == 401
        assert (await client.post("/api/v1/reliability/cameras/any/retry")).status_code == 401

    async def test_the_status_payload_describes_the_camera_without_leaking_it(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(
            camera_id,
            restart_count=4,
            source=RecorderSource.DIRECT.value,
            # The supervisor intentionally retains its last output line. It
            # must not make a currently healthy camera display an old fault.
            last_error="RTP: bad cseq 4711 expected 4710",
        )
        runtime.motion_worker(camera_id)
        await _segment(camera_id, datetime.now(UTC) - timedelta(seconds=30))

        response = await admin_client.get("/api/v1/reliability/cameras")
        assert response.status_code == 200
        payload = response.json()
        entry = payload["cameras"][0]

        assert entry["name"] == "Courtyard"
        assert entry["state"] == "healthy"
        assert entry["recording_fresh"] is True
        assert entry["recorder"]["restart_count"] == 4
        assert entry["recorder"]["source"] == "direct"
        assert entry["recorder"]["running"] is True
        assert entry["recorder"]["fault"] is None
        assert payload["counts"]["healthy"] == 1
        assert entry["recent_incidents"] == []

        body = response.text
        assert "camera-secret" not in body
        assert "192.168.50.50" not in body
        assert "rtsp://" not in body
        for forbidden in ("pid", "command", "argv"):
            assert forbidden not in entry["recorder"]

    async def test_reading_the_status_does_not_write_incidents(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        """Refreshing a browser tab must not move a debounce window."""
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        runtime.no_recorder(camera["id"])
        for _ in range(5):
            assert (await admin_client.get("/api/v1/reliability/cameras")).status_code == 200
        assert await _incidents(camera["id"]) == []

    async def test_incidents_can_be_listed_and_filtered(self, admin_client: AsyncClient) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            session.add_all(
                [
                    CameraIncident(
                        camera_id=camera["id"],
                        reason=IncidentReason.RECORDING_NOT_FRESH,
                        started_at=NOW - timedelta(hours=2),
                        resolved_at=NOW - timedelta(hours=1),
                        duration_seconds=3600.0,
                        detail="No completed segment for 12 minutes.",
                    ),
                    CameraIncident(
                        camera_id=camera["id"],
                        reason=IncidentReason.RELAY_UNAVAILABLE,
                        started_at=NOW - timedelta(minutes=10),
                    ),
                ]
            )
            await session.flush()

        everything = (await admin_client.get("/api/v1/reliability/incidents")).json()
        assert everything["total"] == 2
        assert [item["active"] for item in everything["incidents"]] == [True, False]
        assert everything["incidents"][0]["reason_label"] == "Local stream relay unavailable"

        active = (await admin_client.get("/api/v1/reliability/incidents?active_only=true")).json()
        assert active["total"] == 1
        assert active["incidents"][0]["reason"] == "relay_unavailable"

        recovered = everything["incidents"][1]
        assert recovered["duration_seconds"] == 3600.0
        assert recovered["resolved_at"] is not None


class TestManualRecovery:
    async def test_an_unknown_camera_is_not_found(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post("/api/v1/reliability/cameras/does-not-exist/retry")
        assert response.status_code == 404

    async def test_source_requalification_mutates_and_persists_the_shared_state(
        self, admin_client: AsyncClient
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        manager = get_recording_manager()
        shared = CameraSourceState(
            active=RecorderSource.DIRECT,
            reason=SourceReason.DIRECT_QUALIFIED,
            qualified=True,
            blocked_until={RecorderSource.RELAY.value: NOW + timedelta(hours=1)},
            failed_qualifications={RecorderSource.RELAY.value: 3},
            switch_count=7,
        )
        manager._source_states[camera["id"]] = shared

        response = await admin_client.post(
            f"/api/v1/reliability/cameras/{camera['id']}/source/requalify"
        )
        assert response.status_code == 200
        assert response.json()["reason"] == "operator_reset"
        assert manager._source_states[camera["id"]] is shared
        assert shared.active is RecorderSource.RELAY
        assert shared.blocked_until == {}
        assert shared.switch_count == 0

        async with session_scope() as session:
            persisted = await settings_store.get_setting(
                session, settings_store.RECORDER_SOURCE_POLICY, {}
            )
        assert persisted[camera["id"]]["active"] == "relay"
        assert persisted[camera["id"]]["blocked_until"] == {}

    async def test_a_disabled_camera_is_refused(self, admin_client: AsyncClient) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await admin_client.patch(f"/api/v1/cameras/{camera['id']}", json={"enabled": False})
        response = await admin_client.post(f"/api/v1/reliability/cameras/{camera['id']}/retry")
        assert response.status_code == 409

    async def test_the_result_reports_what_was_done_without_claiming_recovery(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        runtime.no_recorder(camera["id"])

        response = await admin_client.post(f"/api/v1/reliability/cameras/{camera['id']}/retry")
        assert response.status_code == 200
        payload = response.json()
        assert payload["camera_id"] == camera["id"]
        assert payload["accepted"] is True
        assert "relay_stream_refresh_requested" in payload["actions"]
        assert "recorder_restarted" in payload["actions"]
        assert "motion_worker_restarted" in payload["actions"]
        assert payload["state_before"] == "offline"
        assert "not confirmed" in payload["message"]
        # No claim about the state afterwards: a restart outlives the request.
        assert "state_after" not in payload

    async def test_services_that_are_not_configured_are_reported_as_skipped(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await admin_client.patch(
            f"/api/v1/cameras/{camera['id']}",
            json={"recording_mode": "off", "motion_enabled": False},
        )
        runtime.no_recorder(camera["id"])

        payload = (
            await admin_client.post(f"/api/v1/reliability/cameras/{camera['id']}/retry")
        ).json()
        assert "recording_not_configured" in payload["skipped"]
        assert "motion_not_configured" in payload["skipped"]
        assert "recorder_restarted" not in payload["actions"]

    async def test_unavailable_storage_does_not_claim_the_recorder_restarted(
        self,
        admin_client: AsyncClient,
        runtime: Runtime,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        runtime.no_recorder(camera["id"])
        monkeypatch.setattr(
            RecordingManager,
            "suspended_reason",
            property(lambda self: "The recording volume is not mounted."),
        )

        payload = (
            await admin_client.post(f"/api/v1/reliability/cameras/{camera['id']}/retry")
        ).json()
        assert "storage_unavailable" in payload["skipped"]
        assert "recorder_restarted" not in payload["actions"]

    async def test_the_same_camera_cannot_be_retried_in_a_tight_loop(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        runtime.no_recorder(camera["id"])

        first = await admin_client.post(f"/api/v1/reliability/cameras/{camera['id']}/retry")
        assert first.status_code == 200

        second = await admin_client.post(f"/api/v1/reliability/cameras/{camera['id']}/retry")
        assert second.status_code == 429
        assert int(second.headers["Retry-After"]) >= 1

    async def test_the_per_client_rate_limit_stops_a_burst(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        """The router's limiter, independent of the per-camera cooldown."""
        cameras = []
        for index in range(8):
            payload = dict(CAMERA_PAYLOAD)
            payload["name"] = f"Courtyard {index}"
            payload["main_stream_url"] = f"rtsp://192.168.50.5{index}:554/stream1"
            created = await admin_client.post("/api/v1/cameras", json=payload)
            assert created.status_code == 201, created.text
            cameras.append(created.json()["id"])
            runtime.no_recorder(created.json()["id"])

        statuses = [
            (await admin_client.post(f"/api/v1/reliability/cameras/{camera_id}/retry")).status_code
            for camera_id in cameras
        ]
        assert statuses.count(200) == 6
        assert statuses.count(429) == 2

    async def test_the_cooldown_expires(self, admin_client: AsyncClient, runtime: Runtime) -> None:
        from app.database.models import Camera

        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        runtime.no_recorder(camera["id"])
        monitor = get_monitor()

        async with session_scope() as session:
            record = await session.get(Camera, camera["id"])
            assert record is not None
            await monitor.retry_camera(record, now=NOW)
            with pytest.raises(RecoveryThrottledError):
                await monitor.retry_camera(record, now=NOW + timedelta(seconds=10))
            outcome = await monitor.retry_camera(record, now=NOW + timedelta(seconds=120))
        assert outcome.actions


class TestViewerBoundary:
    @staticmethod
    def _as_viewer(client: AsyncClient, settings: Settings, key: str) -> None:
        client.headers.pop("Authorization", None)
        client.cookies.clear()
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr(key)
        client.headers[VIEWER_KEY_HEADER] = key

    async def test_an_unauthenticated_client_gets_nothing(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/viewer/reliability")).status_code == 401

    async def test_a_viewer_sees_a_narrow_summary_and_no_administrator_detail(
        self, admin_client: AsyncClient, settings: Settings, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(
            camera_id,
            restart_count=7,
            source=RecorderSource.DIRECT.value,
            last_error="non-existing PPS 0 referenced",
        )
        runtime.motion_worker(camera_id)
        await _segment(camera_id, datetime.now(UTC) - timedelta(seconds=30))

        self._as_viewer(admin_client, settings, "v" * 48)
        response = await admin_client.get("/api/v1/viewer/reliability")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "private, no-store"

        payload = response.json()
        assert payload["healthy"] == 1
        assert payload["needs_attention"] == 0
        entry = payload["cameras"][0]
        assert set(entry) == {
            "camera_id",
            "name",
            "state",
            "recording_fresh",
            "last_recording_at",
            "issue",
            "issue_label",
            # Coverage is deliberately included: "this camera recorded half the
            # hour" is exactly what an operator needs before trusting the
            # timeline. The *reasons* stay administrator-only, asserted below.
            "coverage_percent",
        }
        assert entry["state"] == "healthy"

        body = response.text
        for forbidden in ("restart_count", "recorder", "source", "fault", "192.168.50.50", "rtsp"):
            assert forbidden not in body

    async def test_a_viewer_cannot_reach_the_administrator_routes(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        self._as_viewer(admin_client, settings, "w" * 48)

        assert (await admin_client.get("/api/v1/reliability/cameras")).status_code == 401
        assert (await admin_client.get("/api/v1/reliability/incidents")).status_code == 401
        retry = await admin_client.post(f"/api/v1/reliability/cameras/{camera['id']}/retry")
        assert retry.status_code == 401

    async def test_a_disabled_camera_is_hidden_from_the_operator_view(
        self, admin_client: AsyncClient, settings: Settings, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        runtime.no_recorder(camera["id"])
        await admin_client.patch(f"/api/v1/cameras/{camera['id']}", json={"enabled": False})

        self._as_viewer(admin_client, settings, "x" * 48)
        payload = (await admin_client.get("/api/v1/viewer/reliability")).json()
        assert payload["cameras"] == []

    async def test_privacy_is_reported_to_the_operator_as_a_pause(
        self, admin_client: AsyncClient, settings: Settings, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        runtime.no_recorder(camera["id"])
        await admin_client.put("/api/v1/system/privacy", json={"enabled": True, "reason": "guests"})

        self._as_viewer(admin_client, settings, "y" * 48)
        payload = (await admin_client.get("/api/v1/viewer/reliability")).json()
        assert payload["privacy_enabled"] is True
        assert "Guests are present" in payload["privacy_message"]
        assert payload["cameras"][0]["state"] == "privacy_paused"
        assert payload["needs_attention"] == 0


async def _segments_covering(
    camera_id: str,
    *,
    end: datetime,
    minutes: int,
    duration_seconds: float = 60.0,
    status: SegmentStatus = SegmentStatus.COMPLETE,
) -> None:
    """Write one segment per minute for the ``minutes`` before ``end``.

    ``duration_seconds`` shorter than sixty is how a starved or restarting
    recorder actually looks on disk: the file exists, its clock-aligned name is
    correct, and most of the minute it names was never recorded.
    """
    for index in range(minutes):
        started = end - timedelta(minutes=minutes - index)
        async with session_scope() as session:
            session.add(
                RecordingSegment(
                    camera_id=camera_id,
                    path=f"{camera_id}/{uuid.uuid4()}.mp4",
                    started_at=started,
                    ended_at=started + timedelta(seconds=duration_seconds),
                    duration_seconds=duration_seconds,
                    size_bytes=6_000_000,
                    status=status,
                )
            )
            await session.flush()


class TestRecordingContinuity:
    """A test installation reported every recorder healthy while one
    camera recorded 50.5% of the hour and another 80.4%. Both had written a
    segment minutes earlier, which is all the previous rule ever checked."""

    async def test_a_camera_recording_half_the_hour_is_not_healthy(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(camera_id, started_at=NOW - timedelta(hours=6))
        runtime.motion_worker(camera_id)
        # Sixty minute-aligned segments, each only thirty seconds long: fresh,
        # continuous-looking by file count, and half the footage missing.
        await _segments_covering(camera_id, end=NOW, minutes=60, duration_seconds=30.0)

        snapshot = await get_monitor().evaluate(now=NOW)
        report = snapshot.cameras[0]

        assert report.assessment.state is not CameraState.HEALTHY
        assert report.assessment.reason is IncidentReason.RECORDING_COVERAGE_LOW
        assert report.assessment.recording_fresh is True
        assert report.telemetry.coverage_ratio is not None
        assert 0.45 < report.telemetry.coverage_ratio < 0.55

    async def test_a_camera_recording_four_fifths_is_degraded_not_healthy(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(camera_id, started_at=NOW - timedelta(hours=6))
        runtime.motion_worker(camera_id)
        await _segments_covering(camera_id, end=NOW, minutes=60, duration_seconds=48.0)

        snapshot = await get_monitor().evaluate(now=NOW)
        report = snapshot.cameras[0]

        assert report.assessment.state is CameraState.DEGRADED
        assert report.assessment.reason is IncidentReason.RECORDING_COVERAGE_LOW

    async def test_a_genuinely_continuous_camera_stays_healthy(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(camera_id, started_at=NOW - timedelta(hours=6))
        runtime.motion_worker(camera_id)
        await _segments_covering(camera_id, end=NOW, minutes=60, duration_seconds=60.0)

        snapshot = await get_monitor().evaluate(now=NOW)
        report = snapshot.cameras[0]

        assert report.assessment.state is CameraState.HEALTHY
        assert report.telemetry.coverage_ratio is not None
        assert report.telemetry.coverage_ratio > 0.95

    async def test_damaged_segments_do_not_count_as_recorded(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        """A file that exists but cannot be read is not footage."""
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(camera_id, started_at=NOW - timedelta(hours=6))
        runtime.motion_worker(camera_id)
        await _segments_covering(camera_id, end=NOW, minutes=60, status=SegmentStatus.DAMAGED)

        snapshot = await get_monitor().evaluate(now=NOW)
        report = snapshot.cameras[0]

        assert report.assessment.state is not CameraState.HEALTHY
        assert report.telemetry.damaged_segments > 0

    async def test_a_newly_added_camera_is_not_accused_of_poor_coverage(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        """A camera enabled ten minutes ago has not had the chance to cover an
        hour, and calling that a fault would simply be false."""
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(camera_id, started_at=NOW - timedelta(minutes=5))
        runtime.motion_worker(camera_id)
        await _segments_covering(camera_id, end=NOW, minutes=4)

        snapshot = await get_monitor().evaluate(now=NOW)
        assert snapshot.cameras[0].assessment.reason is not IncidentReason.RECORDING_COVERAGE_LOW

    async def test_poor_coverage_opens_an_incident_after_the_debounce(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(camera_id, started_at=NOW - timedelta(hours=6))
        runtime.motion_worker(camera_id)
        await _segments_covering(camera_id, end=NOW, minutes=60, duration_seconds=30.0)

        monitor = get_monitor()
        await monitor.evaluate(now=NOW)
        assert await _incidents(camera_id) == []

        await monitor.evaluate(now=NOW + timedelta(seconds=OPEN_AFTER_SECONDS))
        opened = await _incidents(camera_id)
        assert len(opened) == 1
        assert opened[0].reason is IncidentReason.RECORDING_COVERAGE_LOW
        assert opened[0].coverage_ratio is not None
        assert opened[0].coverage_ratio < 0.6

    async def test_a_restart_loop_is_reported_even_while_footage_looks_fine(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.motion_worker(camera_id)
        await _segments_covering(camera_id, end=NOW, minutes=60)

        monitor = get_monitor()
        # Each tick reports a higher supervisor restart total, which is what a
        # camera cycling every couple of minutes actually looks like. Footage
        # keeps arriving throughout, because the point of this rule is to catch
        # a camera whose recordings look fine at a glance.
        for index in range(6):
            moment = NOW + timedelta(seconds=index * 30)
            runtime.recorder(
                camera_id,
                started_at=NOW - timedelta(hours=6),
                restart_count=index,
            )
            await _segments_covering(camera_id, end=moment, minutes=2)
            await monitor.evaluate(now=moment)

        final = NOW + timedelta(seconds=180)
        await _segments_covering(camera_id, end=final, minutes=2)
        snapshot = await monitor.evaluate(now=final)
        report = snapshot.cameras[0]
        assert report.telemetry.restarts_in_window >= 4
        assert report.assessment.state is CameraState.DEGRADED
        assert report.assessment.reason is IncidentReason.RECORDING_UNSTABLE

    async def test_the_admin_payload_carries_the_measurement(
        self, admin_client: AsyncClient, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(camera_id, started_at=datetime.now(UTC) - timedelta(hours=6))
        runtime.motion_worker(camera_id)
        await _segments_covering(
            camera_id, end=datetime.now(UTC), minutes=60, duration_seconds=30.0
        )

        response = await admin_client.get("/api/v1/reliability/cameras")
        assert response.status_code == 200
        quality = response.json()["cameras"][0]["quality"]
        assert quality["coverage_percent"] is not None
        assert quality["coverage_percent"] < 60
        assert quality["gap_count"] > 0
        assert quality["window_seconds"] == 3600.0
        # Still nothing a client should never see.
        body = response.text
        assert "camera-secret" not in body
        assert "rtsp://" not in body

    async def test_the_viewer_sees_the_magnitude_and_none_of_the_internals(
        self, admin_client: AsyncClient, settings: Settings, runtime: Runtime
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        runtime.recorder(camera_id, started_at=datetime.now(UTC) - timedelta(hours=6))
        runtime.motion_worker(camera_id)
        await _segments_covering(
            camera_id, end=datetime.now(UTC), minutes=60, duration_seconds=30.0
        )

        admin_client.headers.pop("Authorization", None)
        admin_client.cookies.clear()
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("c" * 48)
        admin_client.headers[VIEWER_KEY_HEADER] = "c" * 48

        payload = (await admin_client.get("/api/v1/viewer/reliability")).json()
        entry = payload["cameras"][0]
        assert entry["coverage_percent"] is not None
        assert entry["coverage_percent"] < 60
        for forbidden in ("gap_count", "restarts_in_window", "profile", "source", "recorder"):
            assert forbidden not in entry
