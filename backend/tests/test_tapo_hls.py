"""Native HLS uses one camera session and actually emits decodable media."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cameras import tapo_hls
from app.cameras.tapo_battery import TapoBatteryManager, TapoBatteryUnavailableError
from app.cameras.tapo_battery_router import native_live_resource
from app.cameras.tapo_hls import BatteryHlsManager
from app.cameras.tapo_stream import normalise_direct_stream
from app.config import Settings
from app.database.models import Camera, CameraKind
from app.discovery import netsafety
from app.system import privacy


class SyntheticLive:
    """Generate video locally; no camera address or credential is involved."""

    closed = False

    async def close(self):
        self.closed = True

    async def chunks(self, *, max_seconds, ffmpeg_binary, stop):
        source = await asyncio.create_subprocess_exec(
            ffmpeg_binary,
            "-v",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=15",
            "-t",
            "12",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            "15",
            "-f",
            "mpegts",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def responses():
            while chunk := await source.stdout.read(8192):
                # The vendor supplies G.711 outside the video transport. Test
                # both pipes, including that audio cannot deadlock startup.
                yield SimpleNamespace(
                    mimetype="video/mp2t",
                    plaintext=chunk,
                    audioPayload=b"\xd5" * 1600,
                    audioPayloadType=8,
                )

        try:
            async for chunk in normalise_direct_stream(
                responses(), ffmpeg_binary=ffmpeg_binary, should_stop=stop.is_set
            ):
                yield chunk
        finally:
            if source.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    source.kill()
                await source.wait()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe") or os.name == "nt",
    reason="The production audio pipe requires Linux and FFmpeg.",
)
async def test_real_audio_video_pipeline_produces_native_hls_and_releases_everything(
    settings: Settings, monkeypatch
):
    manager = TapoBatteryManager()
    live = SyntheticLive()
    monkeypatch.setattr(manager, "open_live", AsyncMock(return_value=live))
    monkeypatch.setattr(tapo_hls, "get_manager", lambda: manager)
    hls = BatteryHlsManager()
    camera = Camera(id="synthetic", name="Synthetic", kind=CameraKind.TAPO_BATTERY)
    token = await hls.open(camera, settings, owner="test")
    job = hls.resolve(camera.id, token)
    assert job is not None
    directory = job.directory
    try:
        playlist = (directory / "playlist.m3u8").read_text()
        fragment = next(line for line in playlist.splitlines() if line.endswith(".ts"))
        probe = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "json",
            str(directory / fragment),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(probe.communicate(), 5)
        assert probe.returncode == 0, stderr
        codecs = {stream["codec_name"] for stream in json.loads(stdout)["streams"]}
        assert codecs == {"h264", "aac"}
        assert manager.live_active(camera.id)
        assert hls.resolve("other-camera", token) is None
    finally:
        await hls.close(camera.id, token, owner="test")
    assert not manager.live_active(camera.id)
    assert live.closed
    assert not directory.exists()
    assert not hls.sessions


async def test_expired_grant_does_not_serve_files(settings: Settings):
    settings.enable_tapo_battery_provider = True
    with pytest.raises(HTTPException) as result:
        await native_live_resource("camera", "missing", "playlist.m3u8", settings)
    assert result.value.status_code == 404


async def test_unsupported_file_is_never_read(settings: Settings, monkeypatch, tmp_path):
    settings.enable_tapo_battery_provider = True
    monkeypatch.setattr(
        tapo_hls.battery_hls, "resolve", lambda *_: SimpleNamespace(directory=tmp_path)
    )
    (tmp_path / "private.txt").write_text("secret")
    with pytest.raises(HTTPException) as result:
        await native_live_resource("camera", "grant", "private.txt", settings)
    assert result.value.status_code == 404


async def test_failed_open_releases_reservation(settings: Settings, monkeypatch):
    manager = TapoBatteryManager()
    monkeypatch.setattr(manager, "open_live", AsyncMock(side_effect=TimeoutError))
    monkeypatch.setattr(tapo_hls, "get_manager", lambda: manager)
    hls = BatteryHlsManager()
    with pytest.raises(TimeoutError):
        await hls.open(Camera(id="camera"), settings, owner="test")
    assert not manager.live_active("camera")
    assert not hls.sessions


@pytest.mark.parametrize("endpoint", ["live", "hls-session"])
async def test_open_requires_authentication(client, settings, endpoint):
    settings.enable_tapo_battery_provider = True
    response = await client.request(
        "GET" if endpoint == "live" else "POST",
        f"/api/v1/cameras/missing/tapo-battery/{endpoint}",
    )
    assert response.status_code == 401


async def test_privacy_blocks_an_existing_native_grant(settings, monkeypatch):
    settings.enable_tapo_battery_provider = True

    def denied():
        raise HTTPException(status_code=423, detail="Privacy enabled")

    monkeypatch.setattr(privacy, "require_camera_access", denied)
    with pytest.raises(HTTPException) as result:
        await native_live_resource("camera", "grant", "playlist.m3u8", settings)
    assert result.value.status_code == 423


async def test_first_video_failure_returns_json_not_empty_success(
    admin_client, settings, monkeypatch
):
    from app.cameras import tapo_battery_router

    settings.enable_tapo_battery_provider = True
    monkeypatch.setattr(netsafety, "ensure_scannable_host", AsyncMock())
    created = await admin_client.post(
        "/api/v1/cameras",
        json={
            "name": "Test",
            "kind": "tapo_battery",
            "host": "192.168.50.20",
            "tapo_password": "test",
        },
    )
    assert created.status_code == 201, created.text
    manager = TapoBatteryManager()

    class NoVideo:
        close = AsyncMock()

        async def chunks(self, **_kwargs):
            raise TapoBatteryUnavailableError("The camera refused to start video.")
            yield b""  # pragma: no cover - keep this an async generator

    live = NoVideo()
    monkeypatch.setattr(manager, "open_live", AsyncMock(return_value=live))
    monkeypatch.setattr(tapo_battery_router, "get_manager", lambda: manager)
    camera_id = created.json()["id"]
    result = await admin_client.get(f"/api/v1/cameras/{camera_id}/tapo-battery/live")
    assert result.status_code == 424
    assert result.headers["content-type"] == "application/json"
    assert result.json()["detail"] == "The camera refused to start video."
    assert not manager.live_active(camera_id)
    live.close.assert_awaited_once()
