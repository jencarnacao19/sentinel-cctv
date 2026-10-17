"""Browser WebSockets must come from Sentinel's own pages.

A WebSocket handshake is not covered by CORS, and the viewer cookie is
SameSite=None so the Home Assistant iframe keeps working. Without an Origin
check, any other site a household member visits could open the live-stream
socket with that cookie and read the video.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets
from httpx import AsyncClient
from pydantic import SecretStr
from starlette.requests import HTTPConnection
from starlette.types import Scope
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.config import Settings
from app.security.origin import request_origin, websocket_origin_allowed
from app.viewer.auth import VIEWER_COOKIE_NAME

CAMERA_PAYLOAD = {
    "name": "Front door",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
    "username": "camera-admin",
    "password": "camera-secret",
}
VIEWER_KEY = "k" * 48


def _socket(headers: dict[str, str], *, scheme: str = "ws") -> HTTPConnection:
    scope: Scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": scheme,
        "path": "/api/v1/streams/camera/viewer-ws",
        "raw_path": b"/api/v1/streams/camera/viewer-ws",
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "client": ("192.168.50.20", 50000),
        "server": ("192.168.50.27", 8090),
        "subprotocols": [],
    }
    return HTTPConnection(scope)


class TestOriginCheck:
    def test_a_client_without_origin_is_not_a_browser_and_is_allowed(
        self, settings: Settings
    ) -> None:
        assert websocket_origin_allowed(_socket({"host": "192.168.50.27:8090"}), settings)

    def test_a_page_served_by_sentinel_is_allowed(self, settings: Settings) -> None:
        connection = _socket({"host": "192.168.50.27:8090", "origin": "http://192.168.50.27:8090"})
        assert websocket_origin_allowed(connection, settings)

    def test_another_site_is_refused(self, settings: Settings) -> None:
        connection = _socket({"host": "192.168.50.27:8090", "origin": "https://evil.example"})
        assert not websocket_origin_allowed(connection, settings)

    def test_another_port_on_the_same_address_is_refused(self, settings: Settings) -> None:
        connection = _socket({"host": "192.168.50.27:8090", "origin": "http://192.168.50.27:8123"})
        assert not websocket_origin_allowed(connection, settings)

    @pytest.mark.parametrize("origin", ["null", "file://", "chrome-extension://abc"])
    def test_opaque_or_non_web_origins_are_refused(self, settings: Settings, origin: str) -> None:
        connection = _socket({"host": "192.168.50.27:8090", "origin": origin})
        assert not websocket_origin_allowed(connection, settings)

    def test_an_https_page_behind_a_tls_proxy_is_allowed(self, settings: Settings) -> None:
        """The proxy speaks plain ws to Sentinel; the page was https."""
        connection = _socket({"host": "cctv.example.net", "origin": "https://cctv.example.net"})
        assert websocket_origin_allowed(connection, settings)

    def test_a_forwarded_host_from_one_proxy_hop_is_honoured(self, settings: Settings) -> None:
        connection = _socket(
            {
                "host": "127.0.0.1:8090",
                "x-forwarded-host": "cctv.example.net",
                "x-forwarded-proto": "https",
                "origin": "https://cctv.example.net",
            }
        )
        assert websocket_origin_allowed(connection, settings)

    def test_an_explicitly_configured_origin_is_allowed(self, settings: Settings) -> None:
        settings.cors_origins = ["https://dashboard.example.net/"]
        connection = _socket(
            {"host": "192.168.50.27:8090", "origin": "https://dashboard.example.net"}
        )
        assert websocket_origin_allowed(connection, settings)

    def test_the_socket_scheme_maps_to_the_page_scheme(self) -> None:
        assert request_origin(_socket({"host": "cctv.example.net"}, scheme="wss")) == (
            "https://cctv.example.net"
        )


async def _camera_id(admin_client: AsyncClient) -> str:
    response = await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
    assert response.status_code == 201, response.text
    camera_id = response.json()["id"]
    assert isinstance(camera_id, str)
    return camera_id


class TestViewerLiveSocket:
    async def test_another_site_cannot_use_the_viewer_cookie(
        self, ws_server: str, admin_client: AsyncClient, settings: Settings
    ) -> None:
        camera_id = await _camera_id(admin_client)
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr(VIEWER_KEY)

        with pytest.raises(InvalidStatus) as exc_info:
            async with websockets.connect(
                f"ws://{ws_server}/api/v1/streams/{camera_id}/viewer-ws",
                additional_headers={
                    "Cookie": f"{VIEWER_COOKIE_NAME}={VIEWER_KEY}",
                    "Origin": "https://evil.example",
                },
            ):
                pass
        assert exc_info.value.response.status_code == 403

    async def test_sentinels_own_page_still_opens_the_viewer_socket(
        self, ws_server: str, admin_client: AsyncClient, settings: Settings
    ) -> None:
        """The handshake completes; with no go2rtc running in tests the
        proxy then closes with 1011, which proves the origin check passed."""
        camera_id = await _camera_id(admin_client)
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr(VIEWER_KEY)

        async with websockets.connect(
            f"ws://{ws_server}/api/v1/streams/{camera_id}/viewer-ws",
            additional_headers={
                "Cookie": f"{VIEWER_COOKIE_NAME}={VIEWER_KEY}",
                "Origin": f"http://{ws_server}",
            },
        ) as ws:
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=15)
        assert ws.close_code == 1011

    async def test_another_site_cannot_use_the_administrator_cookie(
        self, ws_server: str, admin_client: AsyncClient, settings: Settings
    ) -> None:
        camera_id = await _camera_id(admin_client)
        cookie_value = admin_client.cookies.get(settings.session_cookie_name)
        assert cookie_value

        with pytest.raises(InvalidStatus) as exc_info:
            async with websockets.connect(
                f"ws://{ws_server}/api/v1/streams/{camera_id}/ws",
                additional_headers={
                    "Cookie": f"{settings.session_cookie_name}={cookie_value}",
                    "Origin": "https://evil.example",
                },
            ):
                pass
        assert exc_info.value.response.status_code == 403

    async def test_another_site_cannot_read_the_event_stream(
        self, ws_server: str, admin_client: AsyncClient, settings: Settings
    ) -> None:
        cookie_value = admin_client.cookies.get(settings.session_cookie_name)
        assert cookie_value

        with pytest.raises(InvalidStatus) as exc_info:
            async with websockets.connect(
                f"ws://{ws_server}/api/v1/events/stream",
                additional_headers={
                    "Cookie": f"{settings.session_cookie_name}={cookie_value}",
                    "Origin": "https://evil.example",
                },
            ):
                pass
        assert exc_info.value.response.status_code == 403
