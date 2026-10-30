"""The Home Assistant integration handshake endpoint and install identity."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database.models import Camera, CameraStatus, MotionEvent
from app.database.session import session_scope
from app.integrations.identity import install_id
from app.streaming.config import relay_password

CAMERA_PAYLOAD = {
    "name": "Porch",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
}


@pytest.fixture(autouse=True)
def _mount_integrations_router(app: FastAPI) -> None:
    """``app.api.v1`` does not mount ``app.integrations.router`` yet -- that
    wiring is the orchestrator's job, not something this phase may do itself
    since ``backend/app/api/v1.py`` is off limits here. Every test in this
    file mounts it directly onto the already-lifespan-started ``app``."""
    from app.integrations.router import router as integrations_router

    if not any(getattr(route, "name", None) == "home_assistant_handshake" for route in app.routes):
        app.include_router(integrations_router, prefix="/api/v1")


class TestHandshakeAuth:
    async def test_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/integrations/home-assistant")
        assert response.status_code == 401

    async def test_an_api_token_is_accepted_same_as_a_user_session(
        self, admin_client: AsyncClient
    ) -> None:
        created = await admin_client.post("/api/v1/auth/tokens", json={"name": "ha"})
        token = created.json()["token"]

        response = await admin_client.get(
            "/api/v1/integrations/home-assistant",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200


class TestHandshakeShape:
    async def test_the_documented_fields_are_all_present(self, admin_client: AsyncClient) -> None:
        body = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        assert body["name"] == "Sentinel CCTV"
        assert body["api_version"] == 1
        assert body["websocket_path"] == "/api/v1/events/stream"
        assert isinstance(body["unique_id"], str) and body["unique_id"]
        assert isinstance(body["version"], str) and body["version"]
        assert body["cameras"] == []

    async def test_unique_id_matches_the_install_identity_function(
        self, admin_client: AsyncClient
    ) -> None:
        body = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        assert body["unique_id"] == install_id()

    async def test_unique_id_is_stable_across_repeated_calls(
        self, admin_client: AsyncClient
    ) -> None:
        first = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        second = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        assert first["unique_id"] == second["unique_id"]

    async def test_no_credential_ever_appears_in_the_response(
        self, admin_client: AsyncClient
    ) -> None:
        await admin_client.post(
            "/api/v1/cameras",
            json={**CAMERA_PAYLOAD, "username": "admin", "password": "hunter2"},
        )
        response = await admin_client.get("/api/v1/integrations/home-assistant")
        assert "hunter2" not in response.text


class TestHandshakeLiveStream:
    async def test_there_is_no_stream_url_with_home_assistant_live_off(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        settings.home_assistant_live = False
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        body = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        assert body["cameras"][0]["stream_url"] is None

    async def test_home_assistant_gets_the_relay_substream_on_the_address_it_used(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        created = (
            await admin_client.post(
                "/api/v1/cameras",
                json={
                    **CAMERA_PAYLOAD,
                    "sub_stream_url": "rtsp://192.168.50.50:554/stream2",
                    "username": "admin",
                    "password": "hunter2",
                },
            )
        ).json()

        response = await admin_client.get("/api/v1/integrations/home-assistant")
        camera = next(c for c in response.json()["cameras"] if c["id"] == created["id"])
        stream = f"cam_{created['id'].replace('-', '')}_sub"
        assert camera["stream_url"] == (
            f"rtsp://sentinel:{relay_password(settings)}@sentinel.test:"
            f"{settings.go2rtc_rtsp_port}/{stream}"
        )
        assert "hunter2" not in response.text

    async def test_a_disabled_camera_has_no_stream_url(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        await admin_client.post("/api/v1/cameras", json={**CAMERA_PAYLOAD, "enabled": False})
        body = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        assert body["cameras"][0]["stream_url"] is None


class TestHandshakeCameras:
    async def test_a_freshly_created_camera_is_not_yet_recording(
        self, admin_client: AsyncClient
    ) -> None:
        """Status defaults to offline until the first successful probe; a
        camera Sentinel has never confirmed reachable must not be reported as
        actively recording."""
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()

        body = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        cameras = {camera["id"]: camera for camera in body["cameras"]}
        camera = cameras[created["id"]]

        assert camera["name"] == "Porch"
        assert camera["enabled"] is True
        assert camera["status"] == "offline"
        assert camera["recording_mode"] == "continuous"
        assert camera["recording_state"] == "unavailable"
        assert camera["motion_enabled"] is True
        assert camera["motion_active"] is False
        assert camera["capabilities"] == {
            "snapshot": True,
            "live_stream": True,
            "recording": True,
            "motion_events": True,
        }
        assert camera["stream"] == {
            "codec": None,
            "width": None,
            "height": None,
            "fps": None,
            "audio_codec": None,
            "bitrate_kbps": None,
        }

    async def test_a_disabled_camera_is_reported_off_not_unavailable(
        self, admin_client: AsyncClient
    ) -> None:
        created = (
            await admin_client.post("/api/v1/cameras", json={**CAMERA_PAYLOAD, "enabled": False})
        ).json()

        body = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        camera = next(c for c in body["cameras"] if c["id"] == created["id"])
        assert camera["recording_state"] == "off"
        assert camera["capabilities"]["snapshot"] is False
        assert camera["capabilities"]["live_stream"] is False
        assert camera["capabilities"]["recording"] is False

    async def test_recording_mode_off_is_reported_off_even_when_enabled(
        self, admin_client: AsyncClient
    ) -> None:
        created = (
            await admin_client.post(
                "/api/v1/cameras", json={**CAMERA_PAYLOAD, "recording_mode": "off"}
            )
        ).json()

        body = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        camera = next(c for c in body["cameras"] if c["id"] == created["id"])
        assert camera["recording_state"] == "off"
        assert camera["capabilities"]["recording"] is False

    async def test_motion_only_mode_is_idle_not_recording(self, admin_client: AsyncClient) -> None:
        """Motion-only recording is honestly not implemented yet
        (PROJECT_STATUS, "Known issues") -- reporting it as "recording" would
        claim a camera is being recorded when nothing is happening to it."""
        created = (
            await admin_client.post(
                "/api/v1/cameras", json={**CAMERA_PAYLOAD, "recording_mode": "motion"}
            )
        ).json()
        async with session_scope() as session:
            camera = await session.get(Camera, created["id"])
            assert camera is not None
            camera.status = CameraStatus.ONLINE

        body = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        camera = next(c for c in body["cameras"] if c["id"] == created["id"])
        assert camera["recording_state"] == "idle"
        assert camera["capabilities"]["recording"] is False

    async def test_motion_active_reflects_an_open_motion_event(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()

        async with session_scope() as session:
            session.add(
                MotionEvent(
                    camera_id=created["id"],
                    started_at=datetime.now(UTC),
                    pre_roll_start=datetime.now(UTC),
                )
            )

        body = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        camera = next(c for c in body["cameras"] if c["id"] == created["id"])
        assert camera["motion_active"] is True

        async with session_scope() as session:
            event = (
                await session.execute(
                    select(MotionEvent).where(MotionEvent.camera_id == created["id"])
                )
            ).scalar_one()
            event.ended_at = datetime.now(UTC)
            event.duration_seconds = 0.0

        body = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        camera = next(c for c in body["cameras"] if c["id"] == created["id"])
        assert camera["motion_active"] is False


class TestInstallIdentity:
    def test_is_a_stable_hex_string(self, settings: Settings) -> None:
        first = install_id()
        second = install_id()
        assert first == second
        assert len(first) == 32  # 16 bytes, hex-encoded
        int(first, 16)  # raises ValueError if it is not valid hex

    def test_changes_if_the_install_secret_changes(self, settings: Settings) -> None:
        before = install_id()
        settings.secret_key_path.write_bytes(b"a" * 64)
        after = install_id()
        assert before != after

    def test_never_equals_the_raw_secret(self, settings: Settings) -> None:
        secret = get_settings().load_or_create_secret()
        assert install_id() != secret.decode("ascii", errors="ignore")
