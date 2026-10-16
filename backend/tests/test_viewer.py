"""Read-only viewer access remains isolated from administration."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from starlette.requests import HTTPConnection
from starlette.types import Scope

from app.config import Settings
from app.database.models import RecordingSegment, SegmentStatus
from app.database.session import session_scope
from app.viewer.auth import (
    CF_ACCESS_ASSERTION_HEADER,
    VIEWER_COOKIE_NAME,
    VIEWER_KEY_HEADER,
    VIEWER_SUBPROTOCOL_PREFIX,
    matching_viewer_subprotocol,
)

CAMERA_PAYLOAD = {
    "name": "Porch",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
    "sub_stream_url": "rtsp://192.168.50.50:554/stream2",
    "username": "camera-admin",
    "password": "camera-secret",
}
DAY = datetime(2026, 8, 13, tzinfo=UTC)


def _websocket_connection(protocols: list[str]) -> HTTPConnection:
    scope: Scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "wss",
        "path": "/api/v1/streams/camera/viewer-ws",
        "raw_path": b"/api/v1/streams/camera/viewer-ws",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"sec-websocket-protocol", ", ".join(protocols).encode("ascii"))],
        "client": ("127.0.0.1", 1234),
        "server": ("sentinel.test", 443),
        "subprotocols": protocols,
    }
    return HTTPConnection(scope)


def _viewer_protocol(key: str) -> str:
    encoded = base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")
    return f"{VIEWER_SUBPROTOCOL_PREFIX}{encoded}"


async def _segment(camera_id: str) -> RecordingSegment:
    async with session_scope() as session:
        segment = RecordingSegment(
            camera_id=camera_id,
            path=f"{camera_id}/seg-{uuid.uuid4()}.mp4",
            started_at=DAY,
            ended_at=DAY + timedelta(seconds=60),
            duration_seconds=60.0,
            size_bytes=6_000_000,
            status=SegmentStatus.COMPLETE,
        )
        session.add(segment)
        await session.flush()
        return segment


def test_comma_separated_viewer_hosts_parse_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SENTINEL_VIEWER_TRUSTED_HOSTS",
        "cctv.example.com, sentinel.example.com",
    )
    settings = Settings(_env_file=None)
    assert settings.viewer_trusted_hosts == [
        "cctv.example.com",
        "sentinel.example.com",
    ]


class TestViewerAccess:
    def test_viewer_websocket_key_uses_a_validated_subprotocol(self, settings: Settings) -> None:
        settings.viewer_key = SecretStr("w" * 48)
        valid = _viewer_protocol("w" * 48)
        connection = _websocket_connection(["unrelated.protocol", valid])

        assert matching_viewer_subprotocol(connection, settings) == valid
        assert (
            matching_viewer_subprotocol(
                _websocket_connection([_viewer_protocol("wrong")]),
                settings,
            )
            is None
        )

    async def test_viewer_is_disabled_by_default(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/viewer/cameras")
        assert response.status_code == 401
        assert (await client.get("/api/v1/viewer/system/status")).status_code == 401

    async def test_an_administrator_can_also_use_the_viewer(
        self, admin_client: AsyncClient
    ) -> None:
        response = await admin_client.get("/api/v1/viewer/cameras")
        assert response.status_code == 200

    async def test_a_configured_viewer_key_grants_only_viewer_access(
        self,
        admin_client: AsyncClient,
        settings: Settings,
    ) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        admin_client.headers.pop("Authorization")
        admin_client.cookies.clear()

        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("v" * 48)
        admin_client.headers[VIEWER_KEY_HEADER] = "v" * 48

        response = await admin_client.get("/api/v1/viewer/cameras")
        assert response.status_code == 200
        assert response.json() == [
            {
                "id": response.json()[0]["id"],
                "name": "Porch",
                "kind": "rtsp",
                "status": "offline",
                "has_substream": True,
                "ptz_supported": False,
                "ptz_zoom_supported": False,
                "stream": {
                    "codec": None,
                    "width": None,
                    "height": None,
                    "fps": None,
                    "audio_codec": None,
                    "bitrate_kbps": None,
                },
            }
        ]
        assert "camera-secret" not in response.text
        assert "camera-admin" not in response.text
        assert "192.168.50.50" not in response.text

        system = await admin_client.get("/api/v1/viewer/system/status")
        assert system.status_code == 200
        summary = system.json()
        assert summary["cameras_total"] == 1
        assert summary["storage_total_bytes"] > 0
        assert 0 <= summary["storage_used_percent"] <= 100
        assert summary["configured_retention_days"] == 14
        assert summary["saved_clips_count"] == 0
        assert summary["saved_clips_bytes"] == 0
        assert system.headers["cache-control"] == "private, no-store"
        assert "path" not in summary
        assert "host" not in summary
        assert "192.168.50.50" not in system.text
        assert "camera-secret" not in system.text

        # The same key is not a Principal and cannot enumerate camera settings.
        assert (await admin_client.get("/api/v1/cameras")).status_code == 401
        assert (await admin_client.get("/api/v1/storage/settings")).status_code == 401

    async def test_viewer_can_play_and_preserve_but_not_administer(
        self,
        admin_client: AsyncClient,
        settings: Settings,
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = camera["id"]
        segment = await _segment(camera_id)

        admin_client.headers.pop("Authorization")
        admin_client.cookies.clear()
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("o" * 48)
        admin_client.headers[VIEWER_KEY_HEADER] = "o" * 48

        range_query = {
            "start": DAY.isoformat(),
            "end": (DAY + timedelta(seconds=60)).isoformat(),
        }
        timeline_response = await admin_client.get(
            f"/api/v1/viewer/cameras/{camera_id}/timeline", params=range_query
        )
        assert timeline_response.status_code == 200

        manifest = await admin_client.get(
            f"/api/v1/viewer/cameras/{camera_id}/playback/manifest",
            params=range_query,
        )
        assert manifest.status_code == 200
        assert manifest.json()["items"][0]["url"] == (
            f"/api/v1/viewer/recordings/{segment.id}/download"
        )

        payload = {
            "camera_id": camera_id,
            "start_at": DAY.isoformat(),
            "end_at": (DAY + timedelta(seconds=30)).isoformat(),
        }
        # The viewer cannot ask for an unprotected/expiring export.
        rejected = await admin_client.post(
            "/api/v1/viewer/exports", json={**payload, "protected": False}
        )
        assert rejected.status_code == 422

        created = await admin_client.post("/api/v1/viewer/exports", json=payload)
        assert created.status_code == 201
        export_id = created.json()["id"]
        assert created.json()["protected"] is True

        listed = await admin_client.get("/api/v1/viewer/exports", params={"camera_id": camera_id})
        assert listed.status_code == 200
        assert listed.json()["exports"][0]["id"] == export_id

        # No viewer route exists for destructive mutations, and the admin
        # routes still require a real authenticated Principal.
        assert (await admin_client.delete(f"/api/v1/viewer/exports/{export_id}")).status_code == 405
        assert (
            await admin_client.patch(f"/api/v1/exports/{export_id}", json={"protected": False})
        ).status_code == 401
        camera_edit = await admin_client.patch(f"/api/v1/cameras/{camera_id}", json={})
        assert camera_edit.status_code == 401

    async def test_embedded_key_becomes_a_viewer_only_session_cookie(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("s" * 48)

        response = await client.post(
            "/api/v1/viewer/session",
            headers={VIEWER_KEY_HEADER: "s" * 48},
        )
        assert response.status_code == 204
        assert response.headers["cache-control"] == "no-store"
        assert VIEWER_COOKIE_NAME in client.cookies

        # No key appears in the next request and the cookie still authorises
        # only the narrow viewer surface.
        assert (await client.get("/api/v1/viewer/cameras")).status_code == 200
        assert (await client.get("/api/v1/cameras")).status_code == 401

    async def test_plain_text_embed_session_avoids_a_custom_header(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("t" * 48)

        response = await client.post(
            "/api/v1/viewer/session",
            content="t" * 48,
            headers={"content-type": "text/plain;charset=UTF-8"},
        )

        assert response.status_code == 204
        assert VIEWER_COOKIE_NAME in client.cookies
        assert (await client.get("/api/v1/viewer/cameras")).status_code == 200

    async def test_oversized_plain_text_viewer_key_is_rejected(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("t" * 48)

        response = await client.post(
            "/api/v1/viewer/session",
            content="x" * 513,
            headers={"content-type": "text/plain"},
        )

        assert response.status_code == 413

    async def test_key_in_an_http_query_string_is_not_accepted(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("q" * 48)
        response = await client.get(f"/api/v1/viewer/cameras?viewer_key={'q' * 48}")
        assert response.status_code == 401

    async def test_secure_embed_cookie_works_cross_site_and_clears_old_pwa_once(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("e" * 48)
        settings.cookie_secure = True

        response = await client.post(
            "/api/v1/viewer/session",
            headers={VIEWER_KEY_HEADER: "e" * 48},
        )

        assert response.status_code == 204
        assert response.headers["clear-site-data"] == '"cache"'
        cookies = [value.lower() for value in response.headers.get_list("set-cookie")]
        viewer_cookie = next(value for value in cookies if value.startswith("sentinel_viewer="))
        migration_cookie = next(
            value for value in cookies if value.startswith("sentinel_cache_v3=")
        )
        assert "secure" in viewer_cookie
        assert "samesite=none" in viewer_cookie
        assert "secure" in migration_cookie
        assert "samesite=none" in migration_cookie

    async def test_cache_migration_also_reaches_an_expired_public_viewer(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        settings.viewer_enabled = True
        settings.cookie_secure = True

        response = await client.get("/api/v1/viewer/cameras")

        assert response.status_code == 401
        assert response.headers["clear-site-data"] == '"cache"'

    async def test_cloudflare_access_rejects_an_opaque_assertion_even_on_the_trusted_host(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        settings.viewer_enabled = True
        settings.viewer_trusted_hosts = ["cctv.example.com"]

        assert (
            await client.get(
                "/api/v1/viewer/cameras",
                headers={"host": "cctv.example.com"},
            )
        ).status_code == 401
        assert (
            await client.get(
                "/api/v1/viewer/cameras",
                headers={CF_ACCESS_ASSERTION_HEADER: "opaque-test-assertion"},
            )
        ).status_code == 401

        response = await client.get(
            "/api/v1/viewer/cameras",
            headers={
                "host": "cctv.example.com",
                CF_ACCESS_ASSERTION_HEADER: "opaque-test-assertion",
            },
        )
        assert response.status_code == 401
