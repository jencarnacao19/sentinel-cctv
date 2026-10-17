"""go2rtc configuration generation and the stream proxy."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import stat
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from httpx import AsyncClient
from pydantic import SecretStr

from app.config import Settings
from app.database.models import Camera
from app.database.session import session_scope
from app.streaming import client as client_module
from app.streaming import router as router_module
from app.streaming.config import (
    G711_AUDIO_CODECS,
    SOURCE_SUFFIX,
    build_config,
    build_streams,
    render_config,
    stream_names,
    write_config,
)
from app.streaming.hls import HlsSessionRegistry, hls_sessions
from app.streaming.manager import Go2RtcManager
from app.streaming.manager import get_manager as get_streaming_manager

CAMERA_PAYLOAD = {
    "name": "Porch",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
    "sub_stream_url": "rtsp://192.168.50.50:554/stream2",
    "username": "admin",
    "password": "hunter2",
}


def make_camera(**overrides: object) -> Camera:
    defaults: dict[str, object] = {
        "id": "11111111-2222-3333-4444-555555555555",
        "name": "Porch",
        "host": "192.168.50.50",
        "main_stream_url": "rtsp://192.168.50.50:554/stream1",
        "sub_stream_url": None,
        "username": None,
        "encrypted_password": None,
        "enabled": True,
    }
    defaults.update(overrides)
    return Camera(**defaults)  # type: ignore[arg-type]


class TestStreamNames:
    def test_names_are_derived_from_the_camera_id(self) -> None:
        camera = make_camera()
        main, sub = stream_names(camera)
        assert main == "cam_11111111222233334444555555555555"
        assert sub is None

    def test_a_substream_gets_its_own_name(self) -> None:
        camera = make_camera(sub_stream_url="rtsp://192.168.50.50:554/stream2")
        main, sub = stream_names(camera)
        assert sub == f"{main}_sub"

    def test_names_are_safe_for_a_url_and_a_path(self) -> None:
        camera = make_camera(name="Café terrace / North")
        main, _ = stream_names(camera)
        assert main.isascii()
        assert all(character.isalnum() or character == "_" for character in main)


class TestStreamGeneration:
    def test_a_plain_camera_produces_one_stream(self) -> None:
        streams = build_streams([make_camera()])
        assert streams == {
            "cam_11111111222233334444555555555555": [
                f"rtsp://192.168.50.50:554/stream1{SOURCE_SUFFIX}"
            ]
        }

    def test_backchannel_is_disabled_on_every_source(self) -> None:
        """Consumer cameras advertise two-way audio and then mishandle the SETUP,
        which shows up as a stream that connects and immediately drops."""
        streams = build_streams([make_camera(sub_stream_url="rtsp://192.168.50.50:554/s2")])
        assert all(source[0].endswith("#backchannel=0") for source in streams.values())

    def test_g711_audio_gets_one_lazy_local_aac_source_for_browser_playback(self) -> None:
        camera = make_camera(
            audio_codec="pcm_alaw",
            sub_stream_url="rtsp://192.168.50.50:554/stream2",
        )

        streams = build_streams([camera], go2rtc_host="127.0.0.9", go2rtc_rtsp_port=9554)
        main, sub = stream_names(camera)

        assert set(G711_AUDIO_CODECS) >= {"pcm_alaw", "pcm_mulaw", "pcma", "pcmu"}
        assert streams[main] == [
            f"rtsp://192.168.50.50:554/stream1{SOURCE_SUFFIX}",
            f"ffmpeg:rtsp://127.0.0.9:9554/{main}?video&audio#audio=aac",
        ]
        assert sub is not None
        assert streams[sub][1] == f"ffmpeg:rtsp://127.0.0.9:9554/{sub}?video&audio#audio=aac"

    def test_aac_audio_does_not_get_an_unnecessary_transcoder(self) -> None:
        streams = build_streams([make_camera(audio_codec="aac")])
        assert len(next(iter(streams.values()))) == 1

    def test_disabled_cameras_are_omitted(self) -> None:
        assert build_streams([make_camera(enabled=False)]) == {}

    def test_a_camera_with_no_url_is_skipped_rather_than_breaking_the_config(self) -> None:
        assert build_streams([make_camera(main_stream_url="")]) == {}

    def test_multiple_cameras_get_distinct_names(self) -> None:
        streams = build_streams(
            [
                make_camera(),
                make_camera(id="99999999-8888-7777-6666-555555555555", name="Patio"),
            ]
        )
        assert len(streams) == 2


class TestConfigDocument:
    def test_the_api_binds_to_loopback_only(self, settings: Settings) -> None:
        """go2rtc's admin interface has no authentication of its own; exposing it
        on the LAN would let anyone add a stream pointing at a local file."""
        config = build_config(settings, [])
        assert config["api"]["listen"].startswith("127.0.0.1:")
        assert config["rtsp"]["listen"].startswith("127.0.0.1:")

    def test_the_api_registers_only_the_paths_sentinel_uses(self, settings: Settings) -> None:
        """go2rtc's exit, restart and config endpoints must not exist: a request
        steered to loopback could otherwise stop every recording."""
        api = build_config(settings, [])["api"]
        assert "origin" not in api
        allowed = set(api["allow_paths"])
        assert not allowed & {"/api/exit", "/api/restart", "/api/config", "/api/log"}

        client_source = Path(inspect.getsourcefile(client_module) or "").read_text(encoding="utf-8")
        router_source = Path(inspect.getsourcefile(router_module) or "").read_text(encoding="utf-8")
        used = set(re.findall(r'"(/api(?:/[a-z0-9_.]+)*)"', client_source))
        used |= set(re.findall(r"(/api/ws)\?", router_source))
        assert used, "expected to find the go2rtc paths Sentinel calls"
        assert used <= allowed, f"missing from allow_paths: {sorted(used - allowed)}"

    def test_webrtc_binds_publicly_because_the_browser_must_reach_it(
        self, settings: Settings
    ) -> None:
        config = build_config(settings, [])
        assert config["webrtc"]["listen"].startswith(":")

    def test_ffmpeg_binary_is_taken_from_settings(self, settings: Settings) -> None:
        assert build_config(settings, [])["ffmpeg"]["bin"] == settings.ffmpeg_binary

    def test_rtsp_relay_does_not_filter_g711_audio(self, settings: Settings) -> None:
        assert build_config(settings, [])["rtsp"]["default_query"] == "video&audio"

    def test_the_document_is_valid_yaml(self, settings: Settings) -> None:
        parsed = yaml.safe_load(render_config(settings, [make_camera()]))
        assert "cam_11111111222233334444555555555555" in parsed["streams"]


class TestConfigFile:
    def test_it_is_written_and_reports_a_change(self, settings: Settings) -> None:
        path, changed = write_config(settings, [make_camera()])
        assert changed is True
        assert path.exists()
        assert path == settings.go2rtc_config_path

    def test_rewriting_identical_content_reports_no_change(self, settings: Settings) -> None:
        """An unrelated settings save must not restart every live stream."""
        write_config(settings, [make_camera()])
        _, changed = write_config(settings, [make_camera()])
        assert changed is False

    def test_a_camera_change_is_detected(self, settings: Settings) -> None:
        write_config(settings, [make_camera()])
        _, changed = write_config(
            settings, [make_camera(main_stream_url="rtsp://192.168.50.50:554/other")]
        )
        assert changed is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_permissions_are_restricted_because_it_holds_passwords(
        self, settings: Settings
    ) -> None:
        path, _ = write_config(settings, [make_camera()])
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_no_temporary_file_is_left_behind(self, settings: Settings) -> None:
        path, _ = write_config(settings, [make_camera()])
        assert not path.with_suffix(".yaml.tmp").exists()


class TestConfigFromStoredCameras:
    async def test_stored_credentials_reach_the_generated_config(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        """go2rtc has no other way to authenticate to an RTSP source, so the
        password is necessarily in this file -- which is why it is 0600 and
        excluded from backups and diagnostics."""
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)

        async with session_scope() as session:
            from sqlalchemy import select

            cameras = list((await session.execute(select(Camera))).scalars())

        rendered = render_config(settings, cameras)
        assert "rtsp://admin:hunter2@192.168.50.50:554/stream1" in rendered
        assert "_sub" in rendered

    async def test_camera_changes_rewrite_the_config(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        assert settings.go2rtc_config_path.exists()

        content = settings.go2rtc_config_path.read_text(encoding="utf-8")
        assert "cam_" in content

    async def test_deleting_a_camera_removes_its_stream(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await admin_client.delete(f"/api/v1/cameras/{created['id']}")

        parsed = yaml.safe_load(settings.go2rtc_config_path.read_text(encoding="utf-8"))
        assert parsed["streams"] == {}


class TestHotStreamSync:
    async def test_an_edited_camera_replaces_its_existing_go2rtc_source(
        self, settings: Settings
    ) -> None:
        settings.testing = False
        camera = make_camera(main_stream_url="rtsp://192.168.50.138:554/stream1")
        manager = Go2RtcManager(settings)
        stream_name = camera.stream_name
        manager.client.list_streams = AsyncMock(  # type: ignore[method-assign]
            return_value={stream_name: {"producers": [{"url": "rtsp://192.168.50.220:554/stream1"}]}}
        )
        manager.client.add_stream = AsyncMock()  # type: ignore[method-assign]
        manager.client.delete_stream = AsyncMock()  # type: ignore[method-assign]

        await manager.sync([camera], replace_streams={stream_name})

        manager.client.add_stream.assert_awaited_once_with(
            stream_name,
            [f"rtsp://192.168.50.138:554/stream1{SOURCE_SUFFIX}"],
        )
        manager.client.delete_stream.assert_not_awaited()


class TestStreamEndpoints:
    async def test_info_never_exposes_the_go2rtc_address(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.get(f"/api/v1/streams/{created['id']}")
        assert response.status_code == 200

        body = response.json()
        assert body["websocket_url"] == f"/api/v1/streams/{created['id']}/ws"
        assert body["streams"]["main"].startswith("cam_")
        assert "1984" not in response.text
        assert "127.0.0.1" not in response.text
        assert "hunter2" not in response.text

    async def test_info_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/streams/anything")).status_code == 401

    async def test_unknown_camera_is_404(self, admin_client: AsyncClient) -> None:
        assert (await admin_client.get("/api/v1/streams/nope")).status_code == 404

    async def test_status_reports_readiness(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.get(f"/api/v1/streams/{created['id']}/status")
        assert response.status_code == 200
        assert response.json()["ready"] is False
        assert response.json()["known_to_go2rtc"] is False

    async def test_webrtc_on_a_disabled_camera_is_refused(self, admin_client: AsyncClient) -> None:
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
        response = await admin_client.post(
            f"/api/v1/streams/{created['id']}/webrtc", content=b"v=0"
        )
        assert response.status_code == 409

    async def test_webrtc_reports_503_when_go2rtc_is_down(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.post(
            f"/api/v1/streams/{created['id']}/webrtc", content=b"v=0\r\n"
        )
        assert response.status_code == 503
        assert "not available" in response.json()["detail"]

    async def test_an_oversized_offer_is_refused(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.post(
            f"/api/v1/streams/{created['id']}/webrtc", content=b"x" * (64 * 1024 + 1)
        )
        assert response.status_code == 413

    async def test_viewer_webrtc_uses_the_restricted_viewer_grant(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        admin_client.headers.pop("Authorization")
        admin_client.cookies.clear()

        path = f"/api/v1/streams/{created['id']}/viewer-webrtc"
        assert (await admin_client.post(path, content=b"v=0\r\n")).status_code == 401

        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("i" * 48)
        response = await admin_client.post(
            path,
            content=b"v=0\r\n",
            headers={"X-Sentinel-Viewer-Key": "i" * 48},
        )
        # The test has no go2rtc process. Reaching this guarded exchange proves
        # that the restricted grant was accepted without becoming an admin.
        assert response.status_code == 503

    async def test_hls_proxies_only_the_issued_camera_and_hides_the_upstream_id(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        upstream_id = "upstream-session-123"
        client = get_streaming_manager().client
        monkeypatch.setattr(
            client,
            "hls_master",
            AsyncMock(
                return_value=(
                    b'#EXTM3U\n#EXT-X-STREAM-INF:CODECS="avc1.4D001F"\n'
                    b"hls/playlist.m3u8?id=" + upstream_id.encode()
                )
            ),
        )

        async def resource(kind: str, session_id: str, *, sequence: int | None = None):
            assert session_id == upstream_id
            if kind == "playlist":
                return (
                    b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4?id=upstream-session-123"\n'
                    b"#EXTINF:0.5,\nsegment.m4s?id=upstream-session-123&n=7\n",
                    "application/vnd.apple.mpegurl",
                )
            if kind == "init":
                return b"init", "video/mp4"
            assert kind == "segment" and sequence == 7
            return b"segment", "video/iso.segment"

        monkeypatch.setattr(client, "hls_resource", resource)
        hls_sessions.clear()

        opened = await admin_client.post(f"/api/v1/streams/{created['id']}/hls-session")
        assert opened.status_code == 200, opened.text
        url = opened.json()["url"]
        assert upstream_id not in url
        assert "hunter2" not in url

        playlist = await admin_client.get(url)
        assert playlist.status_code == 200
        assert upstream_id not in playlist.text
        assert 'URI="init.mp4"' in playlist.text
        assert "segment.m4s?n=7" in playlist.text
        assert playlist.headers["cache-control"] == "private, no-store"
        assert (await admin_client.get(url.replace("playlist.m3u8", "init.mp4"))).content == b"init"
        segment = await admin_client.get(
            url.replace("playlist.m3u8", "segment.m4s"), params={"n": 7}
        )
        assert segment.content == b"segment"

        other = (
            await admin_client.post(
                "/api/v1/cameras",
                json={**CAMERA_PAYLOAD, "name": "Other"},
            )
        ).json()
        crossed = url.replace(created["id"], other["id"])
        assert (await admin_client.get(crossed)).status_code == 404

    async def test_viewer_hls_session_uses_the_restricted_viewer_grant(
        self,
        admin_client: AsyncClient,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        admin_client.headers.pop("Authorization")
        admin_client.cookies.clear()
        client = get_streaming_manager().client
        monkeypatch.setattr(
            client,
            "hls_master",
            AsyncMock(return_value=b"#EXTM3U\nhls/playlist.m3u8?id=viewer-upstream"),
        )

        path = f"/api/v1/streams/{created['id']}/viewer-hls-session?sub=true"
        assert (await admin_client.post(path)).status_code == 401

        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("i" * 48)
        response = await admin_client.post(
            path,
            headers={"X-Sentinel-Viewer-Key": "i" * 48},
        )
        assert response.status_code == 200, response.text
        assert "viewer-upstream" not in response.json()["url"]


class TestHlsSessionRegistry:
    def test_grants_are_camera_scoped_and_expire_when_idle(self) -> None:
        now = [100.0]
        registry = HlsSessionRegistry(
            idle_seconds=10,
            max_lifetime_seconds=20,
            clock=lambda: now[0],
        )
        token = registry.issue("camera-1", "upstream-1")

        assert registry.resolve("camera-2", token) is None
        assert registry.resolve("camera-1", token) is not None
        now[0] += 11
        assert registry.resolve("camera-1", token) is None

        active = registry.issue("camera-1", "upstream-2")
        now[0] += 9
        assert registry.resolve("camera-1", active) is not None
        now[0] += 9
        assert registry.resolve("camera-1", active) is not None
        now[0] += 3
        assert registry.resolve("camera-1", active) is None


class TestHealth:
    async def test_callers_can_wait_for_the_existing_startup_probe(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = Go2RtcManager(settings)

        async def alive() -> bool:
            return True

        monkeypatch.setattr(manager.client, "is_alive", alive)
        manager._readiness_task = asyncio.create_task(
            manager._wait_until_ready(timeout_seconds=1.0)
        )
        try:
            assert await manager.wait_until_ready() is True
            assert manager.is_ready is True
        finally:
            await manager.stop()

    async def test_go2rtc_is_skipped_when_no_cameras_are_configured(
        self, client: AsyncClient
    ) -> None:
        """An idle install is healthy, not degraded."""
        body = (await client.get("/api/v1/system/health")).json()
        assert body["status"] == "healthy"
        assert body["components"]["go2rtc"]["state"] == "skipped"
        assert body["go2rtc"] is True

    async def test_go2rtc_is_failed_when_cameras_exist_but_it_is_down(
        self, admin_client: AsyncClient
    ) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        body = (await admin_client.get("/api/v1/system/health")).json()
        assert body["components"]["go2rtc"]["state"] == "failed"
        assert body["go2rtc"] is False
        # Not critical: recording and the rest of the system are unaffected.
        assert body["status"] == "degraded"
