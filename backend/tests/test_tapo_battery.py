"""Local-first boundaries for event-based Tapo battery cameras."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.cameras import tapo_battery
from app.cameras.probe import ProbeResult
from app.cameras.tapo_battery import (
    BatteryChildDevice,
    BatteryStatus,
    BatteryTarget,
    DownloadedClip,
    RemoteClip,
    TapoBatteryAuthError,
    TapoBatteryBusyError,
    TapoBatteryManager,
    TapoBatteryUnavailableError,
    _extract_children,
    _safe_error,
    decode_clips,
    set_manager,
)
from app.cameras.tapo_battery_router import _translate
from app.cameras.tapo_stream import build_normalise_command
from app.config import Settings
from app.database.models import (
    Camera,
    CameraKind,
    RecordingMode,
    RecordingSegment,
    SegmentOrigin,
)
from app.database.session import session_scope
from app.discovery import netsafety
from app.motion.manager import MotionManager
from app.recording.manager import RecordingManager
from app.security.crypto import decrypt_secret
from app.streaming.config import build_streams

BATTERY_PAYLOAD = {
    "name": "Shed",
    "kind": "tapo_battery",
    "host": "192.168.50.20",
    "tapo_password": "local-third-party-secret",
    "tapo_child_id": "8022000000000003",
}


class FakeBatteryManager(TapoBatteryManager):
    def __init__(self) -> None:
        started = datetime.now(UTC) - timedelta(minutes=5)
        self.clip = RemoteClip(
            external_id="tapo:1780000100:1780000112",
            started_at=started,
            ended_at=started + timedelta(seconds=12),
            event_type="person",
        )

    async def status(self, _camera: Camera) -> BatteryStatus:
        return BatteryStatus("TC82", "1.0", "1.2.3", 84, True, "solar", datetime.now(UTC))

    async def test_connection(self, target: BatteryTarget) -> BatteryStatus:
        assert target.host == "192.168.50.20"
        assert target.password == "local-third-party-secret"
        return BatteryStatus(
            "TC82",
            "1.0",
            "1.2.3",
            84,
            True,
            "solar",
            datetime.now(UTC),
            children=(BatteryChildDevice("child_tc82_1", "Backyard", "TC82"),),
        )

    async def clips(self, _camera: Camera, _start: datetime, _end: datetime) -> list[RemoteClip]:
        return [self.clip]

    async def download(self, _camera: Camera, _clip: RemoteClip, output: Path) -> DownloadedClip:
        await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(output.write_bytes, b"fake-local-event-clip")
        return DownloadedClip(output, ProbeResult(codec="h264", width=1920, height=1080, fps=15))

    async def close(self, _camera_id: str) -> None:
        return None

    async def close_all(self) -> None:
        return None


async def _battery_camera() -> Camera:
    async with session_scope() as session:
        return (
            await session.execute(select(Camera).where(Camera.kind == CameraKind.TAPO_BATTERY))
        ).scalar_one()


class TestBatteryCameraCreation:
    async def test_provider_is_disabled_by_default(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post("/api/v1/cameras", json=BATTERY_PAYLOAD)

        assert response.status_code == 422
        assert response.json()["detail"] == "The Tapo battery-camera provider is disabled."

    async def test_battery_camera_is_event_only_and_credentials_stay_encrypted(
        self,
        admin_client: AsyncClient,
        settings: Settings,
        monkeypatch,
    ) -> None:
        settings.enable_tapo_battery_provider = True
        local_check = AsyncMock()
        monkeypatch.setattr(netsafety, "ensure_scannable_host", local_check)

        response = await admin_client.post("/api/v1/cameras", json=BATTERY_PAYLOAD)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "tapo_battery"
        assert body["main_stream_url"] is None
        assert body["recording_mode"] == "events"
        assert body["motion_enabled"] is False
        assert body["username"] == "admin"
        assert body["has_credentials"] is False
        assert body["has_tapo_credentials"] is True
        assert "local-third-party-secret" not in response.text
        local_check.assert_awaited_once_with("192.168.50.20")

        camera = await _battery_camera()
        assert decrypt_secret(camera.encrypted_tapo_password or "") == ("local-third-party-secret")
        assert build_streams([camera]) == {}
        assert RecordingManager(settings)._should_record(camera) is False
        assert MotionManager(settings)._should_detect(camera) is False

    async def test_battery_camera_rejects_rtsp_and_onvif_fields(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        settings.enable_tapo_battery_provider = True
        response = await admin_client.post(
            "/api/v1/cameras",
            json={
                **BATTERY_PAYLOAD,
                "main_stream_url": "rtsp://192.168.50.20/stream1",
                "onvif_profile_token": "profile-1",
            },
        )

        assert response.status_code == 422

    async def test_battery_camera_accepts_an_operator_username(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        settings.enable_tapo_battery_provider = True

        response = await admin_client.post(
            "/api/v1/cameras",
            json={**BATTERY_PAYLOAD, "username": "someone@example.com"},
        )

        assert response.status_code == 201
        assert response.json()["username"] == "someone@example.com"

    async def test_connection_contract_accepts_optional_username_field(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        settings.enable_tapo_battery_provider = True
        set_manager(FakeBatteryManager())
        try:
            response = await admin_client.post(
                "/api/v1/cameras/tapo-battery/test",
                json={
                    "host": "192.168.50.20",
                    "password": "local-third-party-secret",
                    "username": "someone@example.com",
                },
            )
        finally:
            set_manager(TapoBatteryManager())

        assert response.status_code == 200
        assert response.json()["model"] == "TC82"

    async def test_connection_route_accepts_only_host_and_tapo_password(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        settings.enable_tapo_battery_provider = True
        set_manager(FakeBatteryManager())
        try:
            response = await admin_client.post(
                "/api/v1/cameras/tapo-battery/test",
                json={
                    "host": "192.168.50.20",
                    "password": "local-third-party-secret",
                    "child_id": None,
                },
            )
        finally:
            set_manager(TapoBatteryManager())

        assert response.status_code == 200, response.text
        assert response.json()["model"] == "TC82"
        assert response.json()["transport"] == "local_lan"
        assert response.json()["children"] == [
            {"child_id": "child_tc82_1", "name": "Backyard", "model": "TC82"}
        ]

    async def test_connection_route_maps_auth_failure_to_422_with_actionable_message(
        self,
        admin_client: AsyncClient,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings.enable_tapo_battery_provider = True
        manager = TapoBatteryManager()

        async def refuse(_target: BatteryTarget) -> BatteryStatus:
            raise TapoBatteryAuthError(
                "The Tapo battery camera or local hub rejected the credentials. "
                "Enable Third-Party Compatibility in the Tapo app, then use the password "
                "you use to sign in to the Tapo app, not the Camera Account or RTSP password."
            )

        monkeypatch.setattr(manager, "test_connection", refuse)
        set_manager(manager)
        try:
            response = await admin_client.post(
                "/api/v1/cameras/tapo-battery/test",
                json={
                    "host": "192.168.50.20",
                    "password": "wrong-secret",
                    "child_id": None,
                },
            )
        finally:
            set_manager(TapoBatteryManager())

        assert response.status_code == 422, response.text
        assert response.headers["content-type"].startswith("application/json")
        detail = response.json()["detail"]
        assert "rejected the credentials" in detail
        assert "Third-Party Compatibility" in detail
        assert "Tapo app" in detail
        assert "Camera Account" in detail

    async def test_connection_route_maps_unreachable_to_424(
        self,
        admin_client: AsyncClient,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings.enable_tapo_battery_provider = True
        manager = TapoBatteryManager()

        async def unreachable(_target: BatteryTarget) -> BatteryStatus:
            raise TapoBatteryUnavailableError(
                "The Tapo battery camera or local hub did not respond."
            )

        monkeypatch.setattr(manager, "test_connection", unreachable)
        set_manager(manager)
        try:
            response = await admin_client.post(
                "/api/v1/cameras/tapo-battery/test",
                json={
                    "host": "192.168.50.20",
                    "password": "secret",
                    "child_id": None,
                },
            )
        finally:
            set_manager(TapoBatteryManager())

        assert response.status_code == 424, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert "did not respond" in response.json()["detail"]


class TestStatusAndErrorMappings:
    def test_translate_mappings_never_return_502_503_or_401(self) -> None:
        auth_http = _translate(TapoBatteryAuthError("bad password"))
        assert auth_http.status_code == 422

        busy_http = _translate(TapoBatteryBusyError("busy"))
        assert busy_http.status_code == 409

        unavail_http = _translate(TapoBatteryUnavailableError("unreachable"))
        assert unavail_http.status_code == 424

    def test_safe_error_maps_device_confirm_failure_to_auth_error(self) -> None:
        exc = Exception("Incorrect device_confirm value, raising Exception.")
        result = _safe_error(exc)
        assert isinstance(result, TapoBatteryAuthError)
        assert "rejected the saved credentials" in str(result)
        assert "Third-Party Compatibility" in str(result)

    def test_explicit_hub_without_child_is_not_treated_as_standalone(self) -> None:
        assert BatteryTarget("probe", "192.168.50.10", "secret", hub=True).is_standalone is False

    def test_media_401_is_mapped_to_auth_failure(self) -> None:
        result = _safe_error(Exception("HTTP request returned 401 status code"))
        assert isinstance(result, TapoBatteryAuthError)

    def test_extract_children_parsing_from_child_control(self) -> None:
        payload = {
            "childControl": {
                "child_device_list": [
                    {
                        "device_id": "8022000000000001",
                        "device_model": "TC82",
                        "device_alias": "Front Yard",
                    },
                    {
                        "device_id": "8022000000000002",
                        "model": "TC82",
                        "device_name": "Backyard",
                    },
                ]
            }
        }
        children = _extract_children(payload)
        assert len(children) == 2
        assert children[0].child_id == "8022000000000001"
        assert children[0].name == "Front Yard"
        assert children[0].model == "TC82"
        assert children[1].child_id == "8022000000000002"
        assert children[1].name == "Backyard"
        assert children[1].model == "TC82"

    def test_extract_children_returns_empty_on_non_dict_or_error(self) -> None:
        assert _extract_children({}) == []
        assert _extract_children(None) == []
        assert _extract_children({"error_code": -40401}) == []


class TestBatteryPayloadDecoding:
    def test_recordings_are_normalised_and_vendor_labels_are_not_exposed(self) -> None:
        clips = decode_clips(
            {
                "result": {
                    "recording_list": [
                        {
                            "startTime": 1_780_000_000,
                            "endTime": 1_780_000_012,
                            "event_type": "SOME_NEW_PRIVATE_VENDOR_EVENT",
                        },
                        {
                            "start_time": 1_780_000_100,
                            "end_time": 1_780_000_108,
                            "type": "person_detection",
                        },
                    ]
                }
            }
        )

        assert [clip.event_type for clip in clips] == ["person", "camera_event"]
        assert all(clip.started_at.tzinfo is UTC for clip in clips)
        assert clips[0].external_id == "tapo:1780000100:1780000108"

    async def test_only_one_live_session_can_be_reserved_per_camera(self) -> None:
        manager = TapoBatteryManager()
        await manager.reserve_live("tc82")

        with pytest.raises(TapoBatteryBusyError):
            await manager.reserve_live("tc82")

        await manager.release_live("tc82")
        await manager.reserve_live("tc82")
        await manager.release_live("tc82")

    async def test_closing_media_does_not_release_the_owner_reservation(self) -> None:
        manager = TapoBatteryManager()
        media = Mock()
        media.close = AsyncMock()
        await manager.reserve_live("tc82")
        manager._live_media["tc82"] = media

        await manager._close_live_media("tc82")

        media.close.assert_awaited_once()
        with pytest.raises(TapoBatteryBusyError):
            await manager.reserve_live("tc82")
        await manager.release_live("tc82")

    async def test_hub_open_live_completes_the_real_media_handshake_before_returning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = TapoBatteryManager()
        media = Mock()
        media.start = AsyncMock()
        media.close = AsyncMock()
        media.set_window_size = Mock()
        client = Mock()
        client.getMediaSession.return_value = media
        monkeypatch.setattr(manager, "_client", AsyncMock(return_value=client))
        monkeypatch.setattr(
            tapo_battery,
            "_target_from_camera",
            lambda _camera: BatteryTarget("tc82", "192.168.50.20", "secret", "child-1"),
        )
        camera = Camera(
            id="tc82",
            name="Patio",
            kind=CameraKind.TAPO_BATTERY,
            host="192.168.50.20",
        )

        live = await manager.open_live(camera, quality="HD")

        media.start.assert_awaited_once()
        await live.close()
        media.close.assert_awaited_once()

    async def test_standalone_live_bypasses_control_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = TapoBatteryManager()
        media = Mock()
        media.start = AsyncMock()
        media.close = AsyncMock()
        media.set_window_size = Mock()
        monkeypatch.setattr(tapo_battery, "_standalone_media", Mock(return_value=media))
        monkeypatch.setattr(manager, "_ensure_local", AsyncMock())
        monkeypatch.setattr(manager, "_client", AsyncMock())
        monkeypatch.setattr(
            tapo_battery,
            "_target_from_camera",
            lambda _camera: BatteryTarget("tc82", "192.168.50.220", "secret"),
        )
        camera = Camera(
            id="tc82",
            name="Patio",
            kind=CameraKind.TAPO_BATTERY,
            host="192.168.50.220",
        )

        live = await manager.open_live(camera, quality="HD")

        manager._client.assert_not_awaited()
        media.start.assert_awaited_once()
        await live.close()

    async def test_standalone_test_connection_authenticates_on_media_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = TapoBatteryManager()
        media = Mock()
        media.start = AsyncMock()
        media.close = AsyncMock()
        monkeypatch.setattr(tapo_battery, "_standalone_media", Mock(return_value=media))
        monkeypatch.setattr(manager, "_ensure_local", AsyncMock())
        monkeypatch.setattr(manager, "_client", AsyncMock())

        value = await manager.test_connection(BatteryTarget("probe", "192.168.50.220", "secret"))

        assert value.model == "TC82"
        manager._client.assert_not_awaited()
        media.start.assert_awaited_once()
        media.close.assert_awaited_once()

    async def test_standalone_auth_rejection_does_not_retry_another_password_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = TapoBatteryManager()
        rejected = Mock()
        rejected.start = AsyncMock(side_effect=Exception("HTTP request returned 401 status code"))
        rejected.close = AsyncMock()
        media_factory = Mock(return_value=rejected)
        monkeypatch.setattr(tapo_battery, "_standalone_media", media_factory)
        monkeypatch.setattr(manager, "_ensure_local", AsyncMock())

        with pytest.raises(TapoBatteryAuthError):
            await manager.test_connection(BatteryTarget("probe", "192.168.50.220", "secret"))

        assert [call.kwargs["encryption_method"] for call in media_factory.call_args_list] == [
            "sha256",
        ]
        rejected.close.assert_awaited_once()

    async def test_failed_battery_read_is_not_reported_as_online(self) -> None:
        client = Mock()
        client.basicInfo = {"device_model": "TC82"}
        client.getBatteryStatus.side_effect = TimeoutError("vendor details must not escape")
        client.getBatteryStatistic.return_value = {}

        with pytest.raises(TapoBatteryUnavailableError) as caught:
            await TapoBatteryManager()._status(client)

        assert str(caught.value) == "The Tapo battery camera or local hub did not respond."
        assert "vendor details" not in str(caught.value)


class TestBatteryClipSync:
    async def test_manual_sync_is_local_timeline_data_and_idempotent(
        self,
        admin_client: AsyncClient,
        settings: Settings,
        monkeypatch,
    ) -> None:
        settings.enable_tapo_battery_provider = True
        monkeypatch.setattr(netsafety, "ensure_scannable_host", AsyncMock())
        created = await admin_client.post("/api/v1/cameras", json=BATTERY_PAYLOAD)
        assert created.status_code == 201, created.text
        camera_id = created.json()["id"]
        set_manager(FakeBatteryManager())

        before = await admin_client.get(f"/api/v1/cameras/{camera_id}/tapo-battery/clips")
        assert before.status_code == 200, before.text
        assert before.json()[0]["imported"] is False

        first = await admin_client.post(
            f"/api/v1/cameras/{camera_id}/tapo-battery/sync",
            json={"hours": 24, "limit": 5},
        )
        assert first.status_code == 200, first.text
        assert first.json()["imported"] == 1
        assert first.json()["failed"] == 0

        second = await admin_client.post(
            f"/api/v1/cameras/{camera_id}/tapo-battery/sync",
            json={"hours": 24, "limit": 5},
        )
        assert second.status_code == 200, second.text
        assert second.json()["imported"] == 0
        assert second.json()["already_present"] == 1

        async with session_scope() as session:
            segments = (
                (
                    await session.execute(
                        select(RecordingSegment).where(RecordingSegment.camera_id == camera_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(segments) == 1
        assert segments[0].origin is SegmentOrigin.TAPO_EVENT
        assert segments[0].external_id == "tapo:1780000100:1780000112"


def test_events_mode_is_a_real_database_enum_value() -> None:
    mode_type = Camera.__table__.c.recording_mode.type
    assert RecordingMode.EVENTS.name in mode_type.enums


class TestDirectStreamNormalisation:
    def test_video_is_always_normalised_for_browser_playback(self) -> None:
        command = build_normalise_command("custom-ffmpeg", audio_fd=None)

        assert command[0] == "custom-ffmpeg"
        assert command[command.index("-c:v") + 1] == "libx264"
        assert "format=yuv420p" in command[command.index("-vf") + 1]
        assert "-an" in command

    def test_separate_g711_audio_is_joined_as_aac(self) -> None:
        command = build_normalise_command("ffmpeg", audio_fd=9, audio_format="mulaw")

        assert "/dev/fd/9" in command
        assert "mulaw" in command
        assert command[command.index("-c:a") + 1] == "aac"
        assert "1:a:0" in command
