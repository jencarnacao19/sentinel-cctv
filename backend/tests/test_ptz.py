"""ONVIF PTZ discovery, safe movement and operator routes."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import AsyncClient
from pydantic import SecretStr

from app.cameras import ptz
from app.config import Settings
from app.database.models import Camera
from app.database.session import session_scope
from app.viewer.auth import VIEWER_KEY_HEADER

CAMERA_PAYLOAD = {
    "name": "Entrada",
    "main_stream_url": "rtsp://192.168.1.50:554/stream1",
    "username": "camera-user",
    "password": "camera-password",
}

SOAP = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Body>{body}</s:Body>
</s:Envelope>"""

CAPABILITIES = """
<GetCapabilitiesResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
  <Capabilities xmlns:tt="http://www.onvif.org/ver10/schema">
    <tt:Media><tt:XAddr>http://camera.invalid:2020/onvif/service</tt:XAddr></tt:Media>
    <tt:PTZ><tt:XAddr>http://camera.invalid:2020/onvif/service</tt:XAddr></tt:PTZ>
  </Capabilities>
</GetCapabilitiesResponse>
"""

PROFILES = """
<GetProfilesResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
  <Profiles token="profile_1">
    <Name>main</Name>
    <PTZConfiguration token="PTZTOKEN" />
  </Profiles>
</GetProfilesResponse>
"""

STREAM_URI = """
<GetStreamUriResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
  <MediaUri><Uri>rtsp://192.168.1.50:554/stream1</Uri></MediaUri>
</GetStreamUriResponse>
"""

PTZ_OPTIONS = """
<GetConfigurationOptionsResponse xmlns="http://www.onvif.org/ver20/ptz/wsdl">
  <PTZConfigurationOptions>
    <Spaces><ContinuousPanTiltVelocitySpace><URI>pan-tilt</URI></ContinuousPanTiltVelocitySpace></Spaces>
  </PTZConfigurationOptions>
</GetConfigurationOptionsResponse>
"""

PTZ_NODES = """
<GetNodesResponse xmlns="http://www.onvif.org/ver20/ptz/wsdl">
  <PTZNode token="node_1" xmlns:tt="http://www.onvif.org/ver10/schema">
    <tt:MaximumNumberOfPresets>8</tt:MaximumNumberOfPresets>
    <tt:HomeSupported>false</tt:HomeSupported>
    <tt:Extension>
      <tt:SupportedPresetTour>
        <tt:MaximumNumberOfPresetTours>8</tt:MaximumNumberOfPresetTours>
      </tt:SupportedPresetTour>
    </tt:Extension>
  </PTZNode>
</GetNodesResponse>
"""


def make_camera(**overrides: object) -> Camera:
    values: dict[str, object] = {
        "id": "11111111-2222-3333-4444-555555555555",
        "name": "Entrada",
        "host": "192.168.1.50",
        "main_stream_url": "rtsp://192.168.1.50:554/stream1",
        "sub_stream_url": None,
        "username": None,
        "encrypted_password": None,
        "enabled": True,
    }
    values.update(overrides)
    return Camera(**values)  # type: ignore[arg-type]


class TestPtzManager:
    async def test_detection_is_cached_into_a_single_command_target(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            action = request.headers["content-type"]
            if "GetProfiles" in action:
                body = PROFILES
            elif "GetStreamUri" in action:
                body = STREAM_URI
            elif "GetConfigurationOptions" in action:
                body = PTZ_OPTIONS
            elif "GetNodes" in action:
                body = PTZ_NODES
            else:
                body = CAPABILITIES
            return httpx.Response(200, content=SOAP.format(body=body).encode())

        manager = ptz.PtzManager()
        manager._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        camera = make_camera(onvif_profile_token="profile_1")
        try:
            capability = await manager.detect(
                camera,
                device_xaddr="http://192.168.1.50:2020/onvif/device_service",
            )
        finally:
            await manager.stop()

        assert capability.supported is True
        assert capability.ptz_xaddr == "http://192.168.1.50:2020/onvif/service"
        assert capability.profile_token == "profile_1"
        assert capability.zoom_supported is False
        assert capability.preset_capacity == 8
        assert capability.preset_tour_capacity == 8
        assert capability.home_supported is False
        assert len(requests) == 4

    async def test_presets_and_native_patrol_use_only_the_onvif_camera_service(self) -> None:
        actions: list[str] = []
        bodies: list[str] = []
        hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            action = request.headers["content-type"]
            actions.append(action)
            bodies.append(request.content.decode())
            assert request.url.host is not None
            hosts.append(request.url.host)
            if "GetNodes" in action:
                body = PTZ_NODES
            elif "GetPresets" in action:
                body = """
                <GetPresetsResponse xmlns="http://www.onvif.org/ver20/ptz/wsdl">
                  <Preset token="1"><Name>Door</Name></Preset>
                  <Preset token="2"><Name>Driveway</Name></Preset>
                </GetPresetsResponse>
                """
            elif "CreatePresetTour" in action:
                body = """
                <CreatePresetTourResponse xmlns="http://www.onvif.org/ver20/ptz/wsdl">
                  <PresetTourToken>tour_1</PresetTourToken>
                </CreatePresetTourResponse>
                """
            else:
                body = "<ok/>"
            return httpx.Response(200, content=SOAP.format(body=body).encode())

        manager = ptz.PtzManager()
        manager._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        camera = make_camera(
            ptz_supported=True,
            onvif_ptz_xaddr="http://192.168.1.50:2020/onvif/service",
            onvif_profile_token="profile_1",
        )
        try:
            token = await manager.configure_native_patrol(
                camera,
                preset_tokens=["1", "2"],
                stay_seconds=12,
                name="Local patrol",
            )
        finally:
            await manager.stop()

        assert token == "tour_1"
        assert any("CreatePresetTour" in action for action in actions)
        assert any("ModifyPresetTour" in action for action in actions)
        assert any("OperatePresetTour" in action for action in actions)
        assert any("PT12S" in body and "Local patrol" in body for body in bodies)
        assert set(hosts) == {"192.168.1.50"}

    async def test_hold_command_has_camera_and_server_dead_men(self) -> None:
        actions: list[str] = []
        bodies: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            actions.append(request.headers["content-type"])
            bodies.append(request.content.decode())
            return httpx.Response(200, content=SOAP.format(body="<ok/>").encode())

        manager = ptz.PtzManager()
        manager._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        camera = make_camera(
            ptz_supported=True,
            onvif_ptz_xaddr="http://192.168.1.50:2020/onvif/service",
            onvif_profile_token="profile_1",
        )
        try:
            await manager.move(camera, pan=-0.65, tilt=0.25, zoom=0.0)
            await manager.stop_camera(camera)
        finally:
            await manager.stop()

        assert "ContinuousMove" in actions[0]
        assert 'x="-0.650" y="0.250"' in bodies[0]
        assert "<Timeout>" not in bodies[0]
        assert "Stop" in actions[1]

    async def test_camera_without_cached_ptz_is_never_contacted(self) -> None:
        manager = ptz.PtzManager()
        with pytest.raises(ptz.PtzNotConfiguredError):
            await manager.move(make_camera(), pan=1, tilt=0, zoom=0)

    async def test_stop_restores_configured_tapo_patrol_without_blocking_onvif(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        restored: list[str | None] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=SOAP.format(body="<ok/>").encode())

        def fake_restore(target: ptz._ControlTarget) -> None:
            restored.append(target.tapo_patrol_mode)

        monkeypatch.setattr(ptz, "PATROL_RESUME_DELAY_SECONDS", 0)
        monkeypatch.setattr(ptz, "try_decrypt_secret", lambda _value: "camera-password")
        manager = ptz.PtzManager()
        monkeypatch.setattr(manager, "_activate_tapo_patrol_sync", fake_restore)
        manager._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        camera = make_camera(
            username="camera-user",
            encrypted_password="encrypted",
            ptz_supported=True,
            onvif_ptz_xaddr="http://192.168.1.50:2020/onvif/service",
            onvif_profile_token="profile_1",
            tapo_patrol_mode="horizontal",
        )
        try:
            await manager.stop_camera(camera)
            await asyncio.gather(*list(manager._patrol_tasks))
        finally:
            await manager.stop()

        assert restored == ["horizontal"]

    async def test_separate_tapo_password_uses_admin_without_changing_onvif_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def decrypt(value: str | None) -> str | None:
            return {"stream-token": "camera-password", "tapo-token": "cloud-password"}.get(
                value or ""
            )

        monkeypatch.setattr(ptz, "try_decrypt_secret", decrypt)
        camera = make_camera(
            username="camera-user",
            encrypted_password="stream-token",
            encrypted_tapo_password="tapo-token",
            ptz_supported=True,
            onvif_ptz_xaddr="http://192.168.1.50:2020/onvif/service",
            onvif_profile_token="profile_1",
            tapo_patrol_mode="horizontal",
        )

        target = ptz.PtzManager._target(camera)

        assert target.username == "camera-user"
        assert target.password == "camera-password"
        assert target.tapo_username == "admin"
        assert target.tapo_password == "cloud-password"

    async def test_auth_failure_backs_off_instead_of_locking_out_camera(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=SOAP.format(body="<ok/>").encode())

        def fail_authentication(_target: ptz._ControlTarget) -> None:
            nonlocal attempts
            attempts += 1
            raise Exception("Invalid authentication data")

        monkeypatch.setattr(ptz, "PATROL_RESUME_DELAY_SECONDS", 0)
        monkeypatch.setattr(ptz, "try_decrypt_secret", lambda _value: "camera-password")
        manager = ptz.PtzManager()
        monkeypatch.setattr(manager, "_activate_tapo_patrol_sync", fail_authentication)
        manager._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        camera = make_camera(
            username="camera-user",
            encrypted_password="encrypted",
            ptz_supported=True,
            onvif_ptz_xaddr="http://192.168.1.50:2020/onvif/service",
            onvif_profile_token="profile_1",
            tapo_patrol_mode="horizontal",
        )
        try:
            await manager.stop_camera(camera)
            await asyncio.gather(*list(manager._patrol_tasks))
            await manager.stop_camera(camera)
            await asyncio.sleep(0)
        finally:
            await manager.stop()

        assert attempts == 1

    async def test_new_move_invalidates_a_pending_patrol_restore(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        restored: list[str | None] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=SOAP.format(body="<ok/>").encode())

        def fake_restore(target: ptz._ControlTarget) -> None:
            restored.append(target.tapo_patrol_mode)

        monkeypatch.setattr(ptz, "PATROL_RESUME_DELAY_SECONDS", 0.02)
        monkeypatch.setattr(ptz, "try_decrypt_secret", lambda _value: "camera-password")
        manager = ptz.PtzManager()
        monkeypatch.setattr(manager, "_activate_tapo_patrol_sync", fake_restore)
        manager._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        camera = make_camera(
            username="camera-user",
            encrypted_password="encrypted",
            ptz_supported=True,
            onvif_ptz_xaddr="http://192.168.1.50:2020/onvif/service",
            onvif_profile_token="profile_1",
            tapo_patrol_mode="horizontal",
        )
        try:
            await manager.stop_camera(camera)
            patrol_tasks = list(manager._patrol_tasks)
            await manager.move(camera, pan=1, tilt=0, zoom=0)
            await asyncio.gather(*patrol_tasks)
        finally:
            await manager.stop()

        assert restored == []

    async def test_stop_invalidates_older_move_requests_waiting_on_the_camera(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        actions: list[str] = []

        async def fake_soap(
            _client: httpx.AsyncClient,
            _url: str,
            *,
            action: str,
            body: str,
            username: str | None,
            password: str | None,
        ) -> object:
            del body, username, password
            actions.append(action)
            if action.endswith("ContinuousMove"):
                first_started.set()
                await release_first.wait()
            return object()

        monkeypatch.setattr("app.cameras.ptz.onvif._soap_post", fake_soap)
        manager = ptz.PtzManager()
        manager._client = httpx.AsyncClient()
        camera = make_camera(
            ptz_supported=True,
            onvif_ptz_xaddr="http://192.168.1.50:2020/onvif/service",
            onvif_profile_token="profile_1",
        )
        try:
            first = asyncio.create_task(manager.move(camera, pan=1, tilt=0, zoom=0))
            await first_started.wait()
            stale = asyncio.create_task(manager.move(camera, pan=1, tilt=0, zoom=0))
            await asyncio.sleep(0)
            stopped = asyncio.create_task(manager.stop_camera(camera))
            await asyncio.sleep(0)
            release_first.set()
            await asyncio.gather(first, stale, stopped)
        finally:
            await manager.stop()

        assert sum(action.endswith("ContinuousMove") for action in actions) == 1
        assert sum(action.endswith("Stop") for action in actions) == 1


class TestPtzApi:
    async def test_admin_detects_and_persists_ptz(
        self,
        admin_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        detect = AsyncMock(
            return_value=ptz.PtzCapability(
                supported=True,
                message="ONVIF PTZ is ready.",
                device_xaddr="http://192.168.1.50:2020/onvif/device_service",
                ptz_xaddr="http://192.168.1.50:2020/onvif/service",
                profile_token="profile_1",
            )
        )
        monkeypatch.setattr(ptz.get_manager(), "detect", detect)

        response = await admin_client.post(f"/api/v1/cameras/{camera['id']}/ptz/detect")

        assert response.status_code == 200, response.text
        assert response.json()["supported"] is True
        detail = (await admin_client.get(f"/api/v1/cameras/{camera['id']}")).json()
        assert detail["ptz_supported"] is True
        assert detail["onvif_device_xaddr"].endswith("/onvif/device_service")
        assert "profile_1" not in response.text

    async def test_viewer_can_move_but_cannot_detect_or_configure(
        self,
        admin_client: AsyncClient,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            camera = await session.get(Camera, created["id"])
            assert camera is not None
            camera.ptz_supported = True
            camera.onvif_ptz_xaddr = "http://192.168.1.50:2020/onvif/service"
            camera.onvif_profile_token = "profile_1"

        move = AsyncMock()
        stop = AsyncMock()
        monkeypatch.setattr(ptz.get_manager(), "move", move)
        monkeypatch.setattr(ptz.get_manager(), "stop_camera", stop)

        admin_client.headers.pop("Authorization")
        admin_client.cookies.clear()
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("p" * 48)
        admin_client.headers[VIEWER_KEY_HEADER] = "p" * 48

        moved = await admin_client.post(
            f"/api/v1/viewer/cameras/{created['id']}/ptz/move",
            json={"pan": 0.65, "tilt": 0, "zoom": 0},
        )
        stopped = await admin_client.post(f"/api/v1/viewer/cameras/{created['id']}/ptz/stop")

        assert moved.status_code == 204, moved.text
        assert stopped.status_code == 204, stopped.text
        move.assert_awaited_once()
        stop.assert_awaited_once()
        assert (
            await admin_client.post(f"/api/v1/cameras/{created['id']}/ptz/detect")
        ).status_code == 401
        assert (
            await admin_client.patch(f"/api/v1/cameras/{created['id']}", json={"name": "X"})
        ).status_code == 401

    async def test_zero_velocity_is_rejected_before_it_reaches_camera(
        self, admin_client: AsyncClient
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/ptz/move",
            json={"pan": 0, "tilt": 0, "zoom": 0},
        )
        assert response.status_code == 422
