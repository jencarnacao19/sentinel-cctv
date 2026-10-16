"""Global privacy mode is persistent, admin-controlled and enforced server-side."""

from __future__ import annotations

from unittest.mock import AsyncMock

import yaml
from httpx import AsyncClient

from app.cameras.tapo_battery import get_manager as get_tapo_battery_manager
from app.config import Settings
from app.database.session import session_scope
from app.motion.manager import get_manager as get_motion_manager
from app.recording.manager import get_manager as get_recording_manager
from app.streaming.manager import get_manager as get_streaming_manager
from app.system import privacy, settings_store

CAMERA = {
    "name": "Private room",
    "main_stream_url": "rtsp://192.168.1.50:554/stream1",
    "sub_stream_url": "rtsp://192.168.1.50:554/stream2",
}


class TestPrivacyApi:
    async def test_state_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/system/privacy")).status_code == 401
        assert (
            await client.put(
                "/api/v1/system/privacy",
                json={"enabled": True, "reason": "guests"},
            )
        ).status_code == 401

    async def test_enabling_requires_a_predefined_reason(self, admin_client: AsyncClient) -> None:
        missing = await admin_client.put("/api/v1/system/privacy", json={"enabled": True})
        unknown = await admin_client.put(
            "/api/v1/system/privacy",
            json={"enabled": True, "reason": "invented"},
        )
        assert missing.status_code == 422
        assert unknown.status_code == 422

    async def test_enable_blocks_camera_access_and_disable_restores_runtime(
        self, admin_client: AsyncClient, settings: Settings, monkeypatch
    ) -> None:
        close_battery_streams = AsyncMock()
        monkeypatch.setattr(get_tapo_battery_manager(), "close_all", close_battery_streams)
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA)).json()

        enabled = await admin_client.put(
            "/api/v1/system/privacy",
            json={"enabled": True, "reason": "guests"},
        )
        assert enabled.status_code == 200, enabled.text
        state = enabled.json()
        assert state["enabled"] is True
        assert state["reason"] == "guests"
        assert state["reason_label"] == "Guests are present"
        assert "Guests are present" in state["message"]
        assert state["changed_by"] == "operator"
        assert set(state["reasons"]) == {
            "privacy",
            "guests",
            "private_event",
            "maintenance",
            "temporary",
        }

        assert get_streaming_manager().privacy_paused is True
        assert get_recording_manager().privacy_paused is True
        assert get_motion_manager().privacy_paused is True
        close_battery_streams.assert_awaited_once()
        generated = yaml.safe_load(settings.go2rtc_config_path.read_text(encoding="utf-8"))
        assert generated["streams"] == {}

        snapshot = await admin_client.get(f"/api/v1/cameras/{camera['id']}/snapshot")
        webrtc = await admin_client.post(
            f"/api/v1/streams/{camera['id']}/webrtc", content=b"v=0\r\n"
        )
        hls = await admin_client.post(f"/api/v1/streams/{camera['id']}/hls-session")
        stream_test = await admin_client.post(
            "/api/v1/cameras/test-stream",
            json={"url": CAMERA["main_stream_url"]},
        )
        for response in (snapshot, webrtc, hls, stream_test):
            assert response.status_code == 423
            assert "Guests are present" in response.json()["detail"]

        viewer = await admin_client.get("/api/v1/viewer/system/status")
        assert viewer.status_code == 200
        assert viewer.json()["privacy_enabled"] is True
        assert viewer.json()["privacy_reason"] == "guests"
        assert viewer.json()["cameras_online"] == 0

        health = (await admin_client.get("/api/v1/system/health")).json()
        assert health["components"]["go2rtc"]["detail"] == "Paused by privacy mode."
        assert health["components"]["recording"]["data"] == {
            "expected": 0,
            "running": 0,
        }
        assert health["components"]["motion"]["data"] == {
            "expected": 0,
            "running": 0,
        }

        disabled = await admin_client.put("/api/v1/system/privacy", json={"enabled": False})
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["enabled"] is False
        assert get_streaming_manager().privacy_paused is False
        assert get_recording_manager().privacy_paused is False
        assert get_motion_manager().privacy_paused is False
        restored = yaml.safe_load(settings.go2rtc_config_path.read_text(encoding="utf-8"))
        assert len(restored["streams"]) == 2

    async def test_enabled_state_is_loaded_from_persistent_settings(
        self, admin_client: AsyncClient
    ) -> None:
        response = await admin_client.put(
            "/api/v1/system/privacy",
            json={"enabled": True, "reason": "private_event"},
        )
        assert response.status_code == 200

        fresh = privacy.PrivacyManager()
        async with session_scope() as session:
            stored = await settings_store.get_setting(session, settings_store.PRIVACY_MODE)
            await fresh.load(session)

        assert stored["enabled"] is True
        assert stored["reason"] == "private_event"
        assert fresh.state.enabled is True
        assert fresh.state.reason is privacy.PrivacyReason.PRIVATE_EVENT
        assert fresh.state.changed_by == "operator"
