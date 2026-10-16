"""Camera CRUD, credential handling and stream testing."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.cameras import service
from app.cameras.probe import ProbeError, ProbeResult
from app.database.models import (
    Camera,
    CameraStatus,
    RecordingMode,
    RecordingProfile,
    RecordingSourceMode,
)
from app.database.session import session_scope
from app.security.crypto import decrypt_secret

CAMERA_PAYLOAD: dict[str, Any] = {
    "name": "Porch",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
    "sub_stream_url": "rtsp://192.168.50.50:554/stream2",
    "username": "admin",
    "password": "hunter2",
    "manufacturer": "TP-Link",
    "model": "Tapo C210",
}


async def _stored_camera() -> Camera:
    async with session_scope() as session:
        return (await session.execute(select(Camera))).scalar_one()


class TestCreate:
    def test_recording_profile_uses_wire_values_in_the_database(self) -> None:
        """Alembic and the ORM must agree that the stored default is ``auto``."""
        profile_type = Camera.__table__.c.recording_profile.type
        assert profile_type.enums == [member.value for member in RecordingProfile]

    def test_recording_source_mode_uses_wire_values_in_the_database(self) -> None:
        source_type = Camera.__table__.c.recording_source_mode.type
        assert source_type.enums == [member.value for member in RecordingSourceMode]

    async def test_camera_is_created(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["name"] == "Porch"
        assert body["host"] == "192.168.50.50"
        assert body["recording_mode"] == "continuous"
        assert body["recording_profile"] == "auto"
        assert body["recording_source_mode"] == "auto"
        assert body["recording_segment_seconds"] is None
        assert body["status"] == "offline"
        assert body["has_credentials"] is True
        assert body["has_tapo_credentials"] is False
        assert body["tapo_patrol_mode"] is None

    async def test_password_is_never_returned(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        assert "hunter2" not in response.text
        assert "password" not in response.json()

        listing = await admin_client.get("/api/v1/cameras")
        assert "hunter2" not in listing.text

        detail = await admin_client.get(f"/api/v1/cameras/{response.json()['id']}")
        assert "hunter2" not in detail.text

    async def test_password_is_encrypted_at_rest(self, admin_client: AsyncClient) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        camera = await _stored_camera()
        assert camera.encrypted_password is not None
        assert "hunter2" not in camera.encrypted_password
        assert decrypt_secret(camera.encrypted_password) == "hunter2"

    async def test_tapo_control_password_is_write_only_and_encrypted(
        self, admin_client: AsyncClient
    ) -> None:
        response = await admin_client.post(
            "/api/v1/cameras",
            json={**CAMERA_PAYLOAD, "tapo_password": "cloud-secret"},
        )
        assert response.status_code == 201
        assert response.json()["has_tapo_credentials"] is True
        assert "cloud-secret" not in response.text
        assert "tapo_password" not in response.json()

        camera = await _stored_camera()
        assert camera.encrypted_tapo_password is not None
        assert "cloud-secret" not in camera.encrypted_tapo_password
        assert decrypt_secret(camera.encrypted_tapo_password) == "cloud-secret"

    async def test_credentials_embedded_in_the_url_are_extracted(
        self, admin_client: AsyncClient
    ) -> None:
        response = await admin_client.post(
            "/api/v1/cameras",
            json={
                "name": "Patio",
                "main_stream_url": "rtsp://admin:hunter2@192.168.50.51:554/stream1",
            },
        )
        assert response.status_code == 201
        assert response.json()["main_stream_url"] == "rtsp://192.168.50.51:554/stream1"
        assert response.json()["username"] == "admin"
        assert "hunter2" not in response.text

        camera = await _stored_camera()
        assert decrypt_secret(camera.encrypted_password or "") == "hunter2"

    async def test_explicit_password_wins_over_the_embedded_one(
        self, admin_client: AsyncClient
    ) -> None:
        await admin_client.post(
            "/api/v1/cameras",
            json={
                "name": "Carport",
                "main_stream_url": "rtsp://admin:old-password@192.168.50.52:554/s1",
                "password": "new-password",
            },
        )
        camera = await _stored_camera()
        assert decrypt_secret(camera.encrypted_password or "") == "new-password"

    async def test_duplicate_name_is_refused(self, admin_client: AsyncClient) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        response = await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        assert response.status_code == 409

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://192.168.50.50/stream",
            "not-a-url",
            "",
        ],
    )
    async def test_dangerous_or_malformed_urls_are_refused(
        self, admin_client: AsyncClient, url: str
    ) -> None:
        response = await admin_client.post(
            "/api/v1/cameras", json={"name": "Bad", "main_stream_url": url}
        )
        assert response.status_code == 422

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).status_code == 401
        assert (await client.get("/api/v1/cameras")).status_code == 401


class TestReadAndUpdate:
    async def test_list_is_empty_before_anything_is_added(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/v1/cameras")
        assert response.status_code == 200
        assert response.json() == []

    async def test_unknown_camera_is_404(self, admin_client: AsyncClient) -> None:
        assert (await admin_client.get("/api/v1/cameras/nope")).status_code == 404

    async def test_partial_update_leaves_other_fields_alone(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}",
            json={"name": "Main porch", "motion_sensitivity": 75},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Main porch"
        assert body["motion_sensitivity"] == 75
        assert body["main_stream_url"] == created["main_stream_url"]
        assert body["has_credentials"] is True

    async def test_recording_profile_is_explicitly_configurable(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"recording_profile": "normalised"}
        )
        assert response.status_code == 200
        assert response.json()["recording_profile"] == "normalised"
        assert (await _stored_camera()).recording_profile is RecordingProfile.NORMALISED

        rejected = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"recording_profile": "turbo"}
        )
        assert rejected.status_code == 422

    async def test_recording_segment_length_can_override_or_inherit_the_default(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}",
            json={"recording_segment_seconds": 30},
        )
        assert response.status_code == 200
        assert response.json()["recording_segment_seconds"] == 30
        assert (await _stored_camera()).recording_segment_seconds == 30

        inherited = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}",
            json={"recording_segment_seconds": None},
        )
        assert inherited.status_code == 200
        assert inherited.json()["recording_segment_seconds"] is None

        rejected = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}",
            json={"recording_segment_seconds": 9},
        )
        assert rejected.status_code == 422

    async def test_recording_source_can_be_pinned_to_a_local_path(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}",
            json={"recording_source_mode": "relay"},
        )
        assert response.status_code == 200
        assert response.json()["recording_source_mode"] == "relay"
        assert (await _stored_camera()).recording_source_mode is RecordingSourceMode.RELAY

        rejected = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}",
            json={"recording_source_mode": "cloud"},
        )
        assert rejected.status_code == 422

        explicit_null = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}",
            json={"recording_source_mode": None},
        )
        assert explicit_null.status_code == 422

    async def test_omitting_the_password_keeps_the_stored_one(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await admin_client.patch(f"/api/v1/cameras/{created['id']}", json={"name": "Renamed"})
        camera = await _stored_camera()
        assert decrypt_secret(camera.encrypted_password or "") == "hunter2"

    async def test_empty_password_clears_the_credential(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"password": ""}
        )
        assert response.json()["has_credentials"] is False
        camera = await _stored_camera()
        assert camera.encrypted_password is None

    async def test_empty_tapo_password_clears_only_the_control_credential(
        self, admin_client: AsyncClient
    ) -> None:
        created = (
            await admin_client.post(
                "/api/v1/cameras",
                json={**CAMERA_PAYLOAD, "tapo_password": "cloud-secret"},
            )
        ).json()
        response = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"tapo_password": ""}
        )
        assert response.status_code == 200
        assert response.json()["has_credentials"] is True
        assert response.json()["has_tapo_credentials"] is False
        camera = await _stored_camera()
        assert camera.encrypted_password is not None
        assert camera.encrypted_tapo_password is None

    async def test_tapo_patrol_mode_requires_and_preserves_camera_credentials(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        configured = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"tapo_patrol_mode": "horizontal"}
        )
        assert configured.status_code == 200
        assert configured.json()["tapo_patrol_mode"] == "horizontal"

        rejected = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"password": ""}
        )
        assert rejected.status_code == 422

    async def test_tapo_patrol_mode_rejects_unknown_direction(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"tapo_patrol_mode": "diagonal"}
        )
        assert response.status_code == 422

    async def test_disabling_sets_the_disabled_status(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"enabled": False}
        )
        assert response.json()["status"] == "disabled"

        re_enabled = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"enabled": True}
        )
        assert re_enabled.json()["status"] == "offline"

    async def test_unknown_field_is_rejected(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"encrypted_password": "injected"}
        )
        assert response.status_code == 422

    async def test_enabled_only_filter(self, admin_client: AsyncClient) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        await admin_client.post(
            "/api/v1/cameras",
            json={
                "name": "Desligada",
                "main_stream_url": "rtsp://192.168.50.60:554/s1",
                "enabled": False,
            },
        )
        assert len((await admin_client.get("/api/v1/cameras")).json()) == 2
        filtered = await admin_client.get("/api/v1/cameras?enabled_only=true")
        assert [camera["name"] for camera in filtered.json()] == ["Porch"]


class TestDelete:
    async def test_camera_is_removed(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        assert (await admin_client.delete(f"/api/v1/cameras/{created['id']}")).status_code == 204
        assert (await admin_client.get(f"/api/v1/cameras/{created['id']}")).status_code == 404
        assert (await admin_client.get("/api/v1/cameras")).json() == []

    async def test_deleting_an_unknown_camera_is_404(self, admin_client: AsyncClient) -> None:
        assert (await admin_client.delete("/api/v1/cameras/nope")).status_code == 404


class TestStreamResolution:
    async def test_credentials_are_reattached_for_ffmpeg(self, admin_client: AsyncClient) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        camera = await _stored_camera()

        main = service.resolve_stream_url(camera)
        assert main == "rtsp://admin:hunter2@192.168.50.50:554/stream1"
        sub = service.resolve_stream_url(camera, sub=True)
        assert sub == "rtsp://admin:hunter2@192.168.50.50:554/stream2"

    async def test_camera_without_a_substream_resolves_to_none(
        self, admin_client: AsyncClient
    ) -> None:
        await admin_client.post(
            "/api/v1/cameras",
            json={"name": "Simples", "main_stream_url": "rtsp://10.0.0.9:554/s1"},
        )
        camera = await _stored_camera()
        assert service.resolve_stream_url(camera, sub=True) is None


class TestStreamTesting:
    """The probe subprocess is stubbed here; a real FFprobe run is covered in
    test_probe.py."""

    async def test_successful_test_reports_stream_details(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_probe(url: str, **_: object) -> ProbeResult:
            assert url == "rtsp://admin:hunter2@192.168.50.50:554/stream1"
            return ProbeResult(
                codec="h264",
                width=2560,
                height=1440,
                fps=25.0,
                audio_codec="aac",
                bitrate_kbps=4200,
            )

        monkeypatch.setattr("app.cameras.service.probe_stream", fake_probe)
        response = await admin_client.post(
            "/api/v1/cameras/test-stream",
            json={
                "url": "rtsp://192.168.50.50:554/stream1",
                "username": "admin",
                "password": "hunter2",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "ok": True,
            "error": None,
            "codec": "h264",
            "resolution": "2560x1440",
            "width": 2560,
            "height": 1440,
            "fps": 25.0,
            "audio_codec": "aac",
            "bitrate_kbps": 4200,
            "can_stream_copy": True,
        }

    async def test_failure_is_reported_without_leaking_the_password(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_probe(_url: str, **_kwargs: object) -> ProbeResult:
            raise ProbeError("The camera rejected the username or password.")

        monkeypatch.setattr("app.cameras.service.probe_stream", fake_probe)
        response = await admin_client.post(
            "/api/v1/cameras/test-stream",
            json={"url": "rtsp://192.168.50.50:554/s1", "password": "hunter2"},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is False
        assert response.json()["error"] == "The camera rejected the username or password."
        assert "hunter2" not in response.text

    async def test_testing_a_saved_camera_records_what_it_found(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_probe(_url: str, **_kwargs: object) -> ProbeResult:
            return ProbeResult(codec="h264", width=1920, height=1080, fps=20.0)

        monkeypatch.setattr("app.cameras.service.probe_stream", fake_probe)
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()

        response = await admin_client.post(f"/api/v1/cameras/{created['id']}/test")
        assert response.status_code == 200
        assert response.json()["main"]["ok"] is True
        assert response.json()["sub"]["ok"] is True

        detail = (await admin_client.get(f"/api/v1/cameras/{created['id']}")).json()
        assert detail["status"] == "online"
        assert detail["stream"]["codec"] == "h264"
        assert detail["stream"]["width"] == 1920
        assert detail["last_seen_at"] is not None

    async def test_a_failing_main_stream_marks_the_camera_in_error(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_probe(_url: str, **_kwargs: object) -> ProbeResult:
            raise ProbeError("The camera did not respond in time.")

        monkeypatch.setattr("app.cameras.service.probe_stream", fake_probe)
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await admin_client.post(f"/api/v1/cameras/{created['id']}/test")

        detail = (await admin_client.get(f"/api/v1/cameras/{created['id']}")).json()
        assert detail["status"] == "error"
        assert detail["status_message"] == "The camera did not respond in time."

    async def test_a_broken_substream_is_not_fatal(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_probe(url: str, **_kwargs: object) -> ProbeResult:
            if url.endswith("stream2"):
                raise ProbeError("That stream path does not exist.")
            return ProbeResult(codec="h264", width=1920, height=1080, fps=25.0)

        monkeypatch.setattr("app.cameras.service.probe_stream", fake_probe)
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()

        result = (await admin_client.post(f"/api/v1/cameras/{created['id']}/test")).json()
        assert result["main"]["ok"] is True
        assert result["sub"]["ok"] is False

        detail = (await admin_client.get(f"/api/v1/cameras/{created['id']}")).json()
        assert detail["status"] == "online"

    async def test_non_copyable_codec_is_flagged(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_probe(_url: str, **_kwargs: object) -> ProbeResult:
            return ProbeResult(codec="mpeg4", width=640, height=480, fps=15.0)

        monkeypatch.setattr("app.cameras.service.probe_stream", fake_probe)
        response = await admin_client.post(
            "/api/v1/cameras/test-stream", json={"url": "rtsp://10.0.0.5:554/s1"}
        )
        assert response.json()["can_stream_copy"] is False


class TestSnapshot:
    async def test_disabled_camera_cannot_be_snapshotted(self, admin_client: AsyncClient) -> None:
        created = (
            await admin_client.post(
                "/api/v1/cameras",
                json={
                    "name": "Off",
                    "main_stream_url": "rtsp://10.0.0.5:554/s1",
                    "enabled": False,
                },
            )
        ).json()
        response = await admin_client.get(f"/api/v1/cameras/{created['id']}/snapshot")
        assert response.status_code == 409

    async def test_snapshot_returns_jpeg(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_capture(_url: str, **_kwargs: object) -> bytes:
            return b"\xff\xd8\xff\xe0JFIF-ish"

        monkeypatch.setattr("app.cameras.router.capture_snapshot", fake_capture)
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()

        response = await admin_client.get(f"/api/v1/cameras/{created['id']}/snapshot")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content.startswith(b"\xff\xd8")

    async def test_absurd_width_is_rejected(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.get(f"/api/v1/cameras/{created['id']}/snapshot?width=99999")
        assert response.status_code == 422


class TestDefaults:
    async def test_recording_defaults_to_continuous(self, admin_client: AsyncClient) -> None:
        await admin_client.post(
            "/api/v1/cameras",
            json={"name": "Default", "main_stream_url": "rtsp://10.0.0.5:554/s1"},
        )
        camera = await _stored_camera()
        assert camera.recording_mode is RecordingMode.CONTINUOUS
        assert camera.recording_profile is RecordingProfile.AUTO
        assert camera.recording_source_mode is RecordingSourceMode.AUTO
        assert camera.motion_enabled is True
        assert camera.enabled is True
        assert camera.status is CameraStatus.OFFLINE

    async def test_stream_name_is_stable_and_path_safe(self, admin_client: AsyncClient) -> None:
        await admin_client.post(
            "/api/v1/cameras",
            json={"name": "Nome com espaços/barra", "main_stream_url": "rtsp://10.0.0.5:554/s1"},
        )
        camera = await _stored_camera()
        assert camera.stream_name.startswith("cam_")
        assert camera.stream_name.isascii()
        assert "/" not in camera.stream_name
        assert " " not in camera.stream_name
