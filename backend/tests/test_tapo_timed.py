"""Bounded manual recording from a TC82/H200, including every way it ends badly.

The interesting cases here are all failures. A battery camera hangs up on its
own, runs flat mid-session, or is still recording when the server is restarted;
in every one of those the footage captured so far is real and has to reach the
timeline, and the reported outcome has to say what actually happened rather than
what was asked for.

The double replaces the vendor media session -- the one seam that would
otherwise need a physical camera. Everything above it is the real
implementation: the deadline, the writer, the reconnect policy, the remux, the
segment row and the privacy and shutdown hooks all run.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.cameras import tapo_battery, tapo_timed
from app.cameras.probe import probe_stream
from app.cameras.tapo_battery import BatteryStatus, TapoBatteryAuthError, TapoBatteryManager
from app.cameras.tapo_timed import (
    StopReason,
    TimedRecordingManager,
    TimedRecordingState,
)
from app.config import Settings
from app.database.models import (
    Camera,
    CameraKind,
    RecordingSegment,
    SegmentOrigin,
    SegmentStatus,
)
from app.database.session import session_scope
from app.system import privacy

FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for this test")

BATTERY_PAYLOAD = {
    "name": "Shed",
    "kind": "tapo_battery",
    "host": "192.168.50.20",
    "tapo_password": "local-third-party-secret",
}

_TS_PACKET_BYTES = 188


def _synthetic_transport_stream() -> bytes:
    """Six seconds of real H.264 plus audio in MPEG-TS.

    Generated with FFmpeg rather than hand-written, because the point of these
    tests is that what Sentinel writes is *playable*. Fabricated packets would
    remux into a file no decoder accepts, and the tests would pass while the
    feature shipped broken.
    """
    import subprocess
    import tempfile

    binary = shutil.which("ffmpeg")
    assert binary, "guarded by requires_ffmpeg"
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "fixture.ts"
        subprocess.run(  # noqa: S603 - fixed argument list, binary resolved from PATH
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=10:duration=6",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=8000:duration=6",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "10",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-f",
                "mpegts",
                str(output),
            ],
            check=True,
            capture_output=True,
        )
        return output.read_bytes()


_MEDIA: bytes | None = None


def _media() -> bytes:
    global _MEDIA
    if _MEDIA is None:
        _MEDIA = _synthetic_transport_stream()
    return _MEDIA


class FakeLiveSession:
    """Replays real transport-stream bytes, in the aligned chunks a camera sends.

    Substitutes for the vendor media session and nothing else: the writer, the
    deadline, the reconnect policy and the remux above it are all Sentinel's.
    """

    #: Roughly a fifth of a second of the fixture per chunk, packet-aligned.
    CHUNK_PACKETS = 400

    def __init__(
        self,
        *,
        packets: int | None = None,
        chunk_delay: float = 0.005,
    ) -> None:
        self.packets = packets
        self.chunk_delay = chunk_delay
        self.closed = False
        self.yielded = 0
        self.chunk_calls = 0
        self._offset = 0

    def _next_chunk(self) -> bytes:
        media = _media()
        size = self.CHUNK_PACKETS * _TS_PACKET_BYTES
        if self._offset >= len(media):
            # Loop the fixture. A camera left awake keeps sending; the frames
            # repeat but the container stays valid.
            self._offset = 0
        chunk = media[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    async def chunks(
        self,
        *,
        max_seconds: float,
        ffmpeg_binary: str = "ffmpeg",
        no_data_timeout: float = 15.0,
        stop: asyncio.Event | None = None,
    ) -> AsyncIterator[bytes]:
        del ffmpeg_binary, no_data_timeout
        self.chunk_calls += 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_seconds
        try:
            while True:
                if stop is not None and stop.is_set():
                    return
                if loop.time() >= deadline:
                    return
                if privacy.get_manager().state.enabled:
                    return
                if self.packets is not None and self.yielded >= self.packets:
                    # The firmware hung up. Ending the iterator is exactly what
                    # the real session does in that case.
                    return
                self.yielded += 1
                yield self._next_chunk()
                await asyncio.sleep(self.chunk_delay)
        finally:
            await self.close()

    async def close(self) -> None:
        self.closed = True


class FakeBattery(TapoBatteryManager):
    """The vendor seam, and nothing above it."""

    def __init__(
        self,
        *,
        packets_per_open: int | None = None,
        opens_before_failure: int | None = None,
        open_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.packets_per_open = packets_per_open
        self.opens_before_failure = opens_before_failure
        self.open_error = open_error
        self.opens = 0
        self.sessions: list[FakeLiveSession] = []
        self.aborted = 0

    async def status(self, _camera: Camera) -> BatteryStatus:
        return BatteryStatus("TC82", "1.0", "1.2.3", 62, False, None, datetime.now(UTC))

    async def open_live(self, camera: Camera, *, quality: str = "HD") -> Any:
        del camera, quality
        self.opens += 1
        if self.open_error is not None:
            raise self.open_error
        if self.opens_before_failure is not None and self.opens > self.opens_before_failure:
            raise tapo_battery.TapoBatteryUnavailableError(
                "The Tapo battery camera or local hub did not respond."
            )
        session = FakeLiveSession(packets=self.packets_per_open)
        self.sessions.append(session)
        return session

    async def abort_live(self, camera_id: str) -> None:
        self.aborted += 1
        await self.release_live(camera_id)

    async def close_all(self) -> None:
        return None


@pytest.fixture
def battery(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> FakeBattery:
    monkeypatch.setattr(settings, "enable_tapo_battery_provider", True)
    fake = FakeBattery()
    tapo_battery.set_manager(fake)
    return fake


async def _camera(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/api/v1/cameras", json=BATTERY_PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()


async def _orm_camera() -> Camera:
    async with session_scope() as session:
        return (
            await session.execute(select(Camera).where(Camera.kind == CameraKind.TAPO_BATTERY))
        ).scalar_one()


async def _segments() -> list[RecordingSegment]:
    async with session_scope() as session:
        result = await session.execute(
            select(RecordingSegment).where(RecordingSegment.origin == SegmentOrigin.TAPO_MANUAL)
        )
        return list(result.scalars())


class TestDurationBounds:
    @pytest.mark.parametrize("minutes", [5, 10, 15, 30, 60])
    def test_the_offered_presets_are_all_accepted(self, minutes: int) -> None:
        assert tapo_timed.resolve_duration(minutes, configured_max=60) == minutes * 60

    def test_a_custom_duration_within_the_ceiling_is_accepted(self) -> None:
        assert tapo_timed.resolve_duration(37, configured_max=120) == 37 * 60

    def test_zero_and_negative_durations_are_refused(self) -> None:
        for minutes in (0, -5):
            with pytest.raises(tapo_timed.TimedRecordingError):
                tapo_timed.resolve_duration(minutes, configured_max=60)

    def test_the_administrator_ceiling_is_enforced(self) -> None:
        with pytest.raises(tapo_timed.TimedRecordingError, match="at most 30"):
            tapo_timed.resolve_duration(60, configured_max=30)

    def test_the_hard_ceiling_outranks_a_larger_administrator_setting(self) -> None:
        with pytest.raises(tapo_timed.TimedRecordingError, match="at most 120"):
            tapo_timed.resolve_duration(300, configured_max=999)


class TestApiBoundaries:
    async def test_options_report_the_charge_and_the_warning(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.get(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording/options"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["presets_minutes"] == [5, 10, 15, 30, 60]
        assert body["battery_percent"] is None
        assert "flatten the battery" in body["warning"]
        assert body["hard_max_minutes"] == 120

    async def test_the_ceiling_is_administrator_configurable(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        updated = await admin_client.put(
            "/api/v1/cameras/tapo-battery/timed-recording/max-minutes", json={"max_minutes": 15}
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["max_minutes"] == 15
        assert updated.json()["presets_minutes"] == [5, 10, 15]

        refused = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 30, "accept_battery_drain": True},
        )
        assert refused.status_code == 422

    async def test_the_ceiling_cannot_be_set_above_the_hard_limit(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        response = await admin_client.put(
            "/api/v1/cameras/tapo-battery/timed-recording/max-minutes", json={"max_minutes": 500}
        )
        assert response.status_code == 422

    async def test_starting_without_accepting_the_battery_cost_is_refused(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 5, "accept_battery_drain": False},
        )
        assert response.status_code == 400
        assert "battery cost" in response.json()["detail"]
        assert battery.opens == 0, "an unconfirmed request must not wake the camera"

    async def test_an_authentication_failure_is_reported_as_such(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        battery.open_error = TapoBatteryAuthError(
            "The Tapo battery camera or local hub rejected the credentials."
        )
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 5, "accept_battery_drain": True},
        )
        # Credential rejection is a correctable input error, not a broken
        # origin. Keeping it in the 4xx family prevents Cloudflare replacing
        # Sentinel's useful message with a generic Bad Gateway page.
        assert response.status_code == 422
        assert "rejected the credentials" in response.json()["detail"]
        assert tapo_timed.get_manager().status(camera["id"]) is None

    async def test_an_rtsp_camera_cannot_start_one(self, admin_client: AsyncClient) -> None:
        created = await admin_client.post(
            "/api/v1/cameras",
            json={
                "name": "Carport",
                "main_stream_url": "rtsp://192.168.50.217:554/stream1",
                "username": "camera-admin",
                "password": "camera-secret",
            },
        )
        response = await admin_client.post(
            f"/api/v1/cameras/{created.json()['id']}/tapo-battery/timed-recording",
            json={"minutes": 5, "accept_battery_drain": True},
        )
        assert response.status_code in (404, 409)

    async def test_starting_is_administrator_only(
        self, app: Any, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        from httpx import ASGITransport

        camera = await _camera(admin_client)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://sentinel.test") as anonymous:
            response = await anonymous.post(
                f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
                json={"minutes": 5, "accept_battery_drain": True},
            )
        assert response.status_code == 401
        assert battery.opens == 0


class TestSessionLifecycle:
    async def test_starting_returns_immediately_rather_than_holding_the_request(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        """A sixty-minute operation must not be a sixty-minute HTTP request."""
        camera = await _camera(admin_client)
        loop = asyncio.get_running_loop()
        began = loop.time()
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 60, "accept_battery_drain": True},
        )
        elapsed = loop.time() - began
        assert response.status_code == 202, response.text
        assert elapsed < 5.0, "the start request must return long before any proxy times out"
        assert response.json()["state"] == "recording"
        # A standalone TC82 normally sleeps and exposes no control endpoint.
        # Starting Direct Stream must not add a second status login merely to
        # populate telemetry; H200 child cameras can report this value.
        assert response.json()["battery_percent_at_start"] is None

        stopped = await admin_client.delete(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording"
        )
        assert stopped.status_code == 200

    async def test_progress_is_polled_without_touching_the_camera(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 10, "accept_battery_drain": True},
        )
        opens = battery.opens
        for _ in range(5):
            response = await admin_client.get(
                f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording"
            )
            assert response.status_code == 200
            assert response.json()["state"] in {"recording", "reconnecting"}
            assert response.json()["seconds_remaining"] <= 600
        assert battery.opens == opens, "polling must not open a vendor session"

        await admin_client.delete(f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording")

    async def test_only_one_session_per_camera(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        first = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 10, "accept_battery_drain": True},
        )
        assert first.status_code == 202
        second = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 10, "accept_battery_drain": True},
        )
        assert second.status_code == 409
        await admin_client.delete(f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording")

    async def test_a_live_view_blocks_a_timed_recording_and_vice_versa(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        await battery.reserve_live(camera["id"])
        try:
            response = await admin_client.post(
                f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
                json={"minutes": 5, "accept_battery_drain": True},
            )
            assert response.status_code == 409
        finally:
            await battery.release_live(camera["id"])

    async def test_stopping_a_session_that_is_not_running_is_a_not_found(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.delete(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording"
        )
        assert response.status_code == 404


@requires_ffmpeg
class TestCapturedMedia:
    """Every test here asserts on a real file that FFmpeg produced."""

    async def test_a_manual_stop_keeps_and_indexes_what_was_captured(
        self, admin_client: AsyncClient, battery: FakeBattery, recordings_dir: Path
    ) -> None:
        camera = await _camera(admin_client)
        await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 60, "accept_battery_drain": True},
        )
        await asyncio.sleep(0.4)
        stopped = await admin_client.delete(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording"
        )
        assert stopped.status_code == 200, stopped.text
        body = stopped.json()
        assert body["state"] == "partial", "a few seconds of a sixty-minute request is partial"
        assert body["stop_reason"] == "operator"
        assert body["bytes_recorded"] > 0
        assert body["segment_id"]

        segments = await _segments()
        assert len(segments) == 1
        segment = segments[0]
        assert segment.origin is SegmentOrigin.TAPO_MANUAL
        assert segment.camera_id == camera["id"]
        assert (recordings_dir / segment.path).exists()
        assert (recordings_dir / segment.path).stat().st_size > 0
        assert segment.path.endswith(".mp4")
        probe = await probe_stream(str(recordings_dir / segment.path))
        assert probe.audio_codec == "aac"
        leftovers = await asyncio.to_thread(lambda: list(recordings_dir.rglob("*.part.ts")))
        assert leftovers == [], "the part file is cleaned up"

    async def test_the_clip_appears_in_the_timeline_and_plays_back(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 60, "accept_battery_drain": True},
        )
        await asyncio.sleep(0.4)
        stopped = await admin_client.delete(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording"
        )
        segment_id = stopped.json()["segment_id"]
        assert segment_id

        listing = await admin_client.get(
            "/api/v1/recordings", params={"camera_id": camera["id"], "limit": 50}
        )
        assert listing.status_code == 200, listing.text
        assert any(item["id"] == segment_id for item in listing.json()["segments"])

        # The same manifest Home Assistant and the Sentinel timeline both read.
        manifest = await admin_client.get(
            f"/api/v1/cameras/{camera['id']}/playback/manifest",
            params={
                "start": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                "end": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            },
        )
        assert manifest.status_code == 200, manifest.text
        assert any(item["segment_id"] == segment_id for item in manifest.json()["items"])

        playback = await admin_client.get(f"/api/v1/recordings/{segment_id}/download")
        assert playback.status_code in (200, 206), playback.text
        assert len(playback.content) > 0

    async def test_the_firmware_hanging_up_produces_a_bounded_local_reconnect(
        self, admin_client: AsyncClient, battery: FakeBattery, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tapo_timed, "RECONNECT_DELAYS_SECONDS", (0.05,))
        battery.packets_per_open = 3
        camera = await _camera(admin_client)
        await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 60, "accept_battery_drain": True},
        )
        await asyncio.sleep(0.6)
        status = tapo_timed.get_manager().status(camera["id"])
        assert status is not None
        assert status.reconnects >= 1, "an early close must be reopened, not abandoned"
        assert battery.opens >= 2

        stopped = await admin_client.delete(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording"
        )
        assert stopped.json()["state"] == "partial"
        assert stopped.json()["reconnects"] >= 1

    async def test_a_camera_that_powers_off_saves_the_partial_and_says_so(
        self, admin_client: AsyncClient, battery: FakeBattery, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The battery went flat. The frames already captured are still real."""
        monkeypatch.setattr(tapo_timed, "RECONNECT_DELAYS_SECONDS", (0.02,))
        monkeypatch.setattr(tapo_timed, "MAX_CONSECUTIVE_RECONNECT_FAILURES", 2)
        battery.packets_per_open = 20
        battery.opens_before_failure = 1
        camera = await _camera(admin_client)
        started = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 60, "accept_battery_drain": True},
        )
        assert started.status_code == 202

        manager = tapo_timed.get_manager()
        for _ in range(100):
            status = manager.status(camera["id"])
            if status is not None and status.finished:
                break
            await asyncio.sleep(0.1)
        status = manager.status(camera["id"])
        assert status is not None and status.finished, "the session must end on its own"
        assert status.stop_reason is StopReason.CAMERA_UNREACHABLE
        assert status.state is TimedRecordingState.PARTIAL
        assert battery.sessions[0].chunk_calls == 1, (
            "a failed reconnect must not drain the already closed session again"
        )
        assert "powered off before the requested duration" in status.message
        assert "60" in status.message, "the requested duration is still reported honestly"
        assert str(status.requested_seconds // 60) in status.message

        segments = await _segments()
        assert len(segments) == 1, "the partial clip is kept"
        assert segments[0].duration_seconds is not None
        assert segments[0].duration_seconds < 3600

    async def test_privacy_mode_ends_the_session_immediately(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 60, "accept_battery_drain": True},
        )
        await asyncio.sleep(0.3)

        await tapo_timed.get_manager().set_privacy_paused(True)
        status = tapo_timed.get_manager().status(camera["id"])
        assert status is not None
        assert status.finished
        assert status.stop_reason is StopReason.PRIVACY
        assert status.state is TimedRecordingState.PARTIAL
        assert len(await _segments()) == 1, "privacy stops recording; it does not destroy footage"

    async def test_privacy_mode_refuses_to_start_a_new_session(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        await tapo_timed.get_manager().set_privacy_paused(True)
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 5, "accept_battery_drain": True},
        )
        assert response.status_code in (409, 422, 423)
        assert battery.opens == 0

    async def test_shutdown_closes_the_camera_and_preserves_the_partial_clip(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        camera = await _camera(admin_client)
        await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/timed-recording",
            json={"minutes": 60, "accept_battery_drain": True},
        )
        await asyncio.sleep(0.3)

        await tapo_timed.get_manager().stop()

        status = tapo_timed.get_manager().status(camera["id"])
        assert status is not None
        assert status.finished
        assert status.stop_reason is StopReason.SHUTDOWN
        assert status.state is TimedRecordingState.PARTIAL
        assert battery.aborted >= 1, "the camera session is closed, not abandoned"
        segments = await _segments()
        assert len(segments) == 1
        assert segments[0].duration_seconds is not None

    async def test_an_interrupted_run_is_finalised_at_the_next_start(
        self, admin_client: AsyncClient, battery: FakeBattery, recordings_dir: Path
    ) -> None:
        """A hard kill leaves a part file. It is still footage."""
        camera = await _camera(admin_client)
        started_at = datetime.now(UTC).replace(microsecond=0)
        relative_output, relative_part = tapo_timed._relative_paths(camera["id"], started_at)
        part = recordings_dir / relative_part
        part.parent.mkdir(parents=True, exist_ok=True)
        # Real media, truncated the way a hard kill truncates it.
        media = _media()
        part.write_bytes(media[: int(len(media) * 0.6) // _TS_PACKET_BYTES * _TS_PACKET_BYTES])

        await TimedRecordingManager().start()

        assert not part.exists(), "the part file is consumed"
        assert (recordings_dir / relative_output).exists()
        segments = await _segments()
        assert len(segments) == 1
        assert segments[0].path == relative_output
        assert segments[0].origin is SegmentOrigin.TAPO_MANUAL

    async def test_an_unreadable_part_file_is_discarded_on_the_second_attempt(
        self, admin_client: AsyncClient, battery: FakeBattery, recordings_dir: Path
    ) -> None:
        """A leak nothing accounts for: retention sums indexed segments, not files."""
        camera = await _camera(admin_client)
        _, relative_part = tapo_timed._relative_paths(camera["id"], datetime.now(UTC))
        part = recordings_dir / relative_part
        part.parent.mkdir(parents=True, exist_ok=True)
        # Large enough to be worth remuxing, but not a transport stream.
        part.write_bytes(b"not a transport stream " * 4000)

        await TimedRecordingManager().start()

        assert not part.exists(), "an unremuxable file must not be left to accumulate"
        assert await _segments() == []

    async def test_recovery_never_overwrites_a_clip_already_in_the_timeline(
        self, admin_client: AsyncClient, battery: FakeBattery, recordings_dir: Path
    ) -> None:
        camera = await _camera(admin_client)
        started_at = datetime.now(UTC).replace(microsecond=0)
        relative_output, relative_part = tapo_timed._relative_paths(camera["id"], started_at)

        # A finished clip, indexed, whose part file was somehow left behind.
        output = recordings_dir / relative_output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"the original indexed clip")
        part = recordings_dir / relative_part
        media = _media()
        part.write_bytes(media[: len(media) // 2 // _TS_PACKET_BYTES * _TS_PACKET_BYTES])

        async with session_scope() as db:
            db.add(
                RecordingSegment(
                    camera_id=camera["id"],
                    path=relative_output,
                    origin=SegmentOrigin.TAPO_MANUAL,
                    external_id=f"manual:{int(started_at.timestamp())}",
                    started_at=started_at,
                    ended_at=started_at,
                    duration_seconds=3.0,
                    size_bytes=output.stat().st_size,
                    status=SegmentStatus.COMPLETE,
                )
            )

        await TimedRecordingManager().start()

        assert output.read_bytes() == b"the original indexed clip", "the indexed file is untouched"
        assert not part.exists(), "the stale marker is cleared"
        assert len(await _segments()) == 1, "no duplicate row"

    async def test_a_part_file_too_small_to_play_is_discarded_not_indexed(
        self, admin_client: AsyncClient, battery: FakeBattery, recordings_dir: Path
    ) -> None:
        camera = await _camera(admin_client)
        _, relative_part = tapo_timed._relative_paths(camera["id"], datetime.now(UTC))
        part = recordings_dir / relative_part
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(_media()[:_TS_PACKET_BYTES])

        await TimedRecordingManager().start()

        assert not part.exists()
        assert await _segments() == [], "an unplayable fragment must not enter the timeline"


class TestNoAutomaticRecording:
    async def test_nothing_starts_a_session_on_its_own(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        """The camera must be untouched until a person asks."""
        camera = await _camera(admin_client)
        await tapo_timed.get_manager().start()
        await asyncio.sleep(0.2)
        assert battery.opens == 0
        assert tapo_timed.get_manager().status(camera["id"]) is None
        assert await _segments() == []

    async def test_a_battery_camera_never_joins_the_continuous_recorder(
        self, admin_client: AsyncClient, battery: FakeBattery
    ) -> None:
        from app.recording.manager import get_manager as get_recording_manager

        await _camera(admin_client)
        camera = await _orm_camera()
        assert get_recording_manager()._should_record(camera) is False
        assert get_recording_manager().camera_recording_state(camera) == "unsupported"
