"""Viewer accounts watch through the viewer and reach nothing else.

They are the first accounts that are not administrators, and every route
written before them assumed a signed-in person was one. The sweep below is what
keeps that assumption from turning into access: to every route outside the
viewer and the account's own profile, a viewer account must get exactly what a
client with no credentials gets.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

import pytest
import websockets
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .conftest import ADMIN_PASSWORD, ADMIN_USERNAME

VIEWER = {"username": "amy", "password": "family-password-1"}
CAMERA = {
    "name": "Front door",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
    "username": "camera-admin",
    "password": "camera-secret",
}

#: Routes left out of the OpenAPI schema. Their own URL token authorises them.
UNLISTED_ROUTES = {
    "/api/v1/streams/{camera_id}/hls/{token}/playlist.m3u8": {"get"},
    "/api/v1/streams/{camera_id}/hls/{token}/init.mp4": {"get"},
    "/api/v1/streams/{camera_id}/hls/{token}/segment.m4s": {"get"},
}

#: The viewer's own routes outside /viewer: its live-stream handshakes.
VIEWER_STREAM_ROUTES = {
    "/api/v1/streams/{camera_id}/viewer-webrtc",
    "/api/v1/streams/{camera_id}/viewer-hls-session",
}

#: Routes every account uses for itself. Each is covered by its own test below.
ACCOUNT_ROUTES = {
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/status",
    "/api/v1/auth/setup",
    "/api/v1/auth/me",
    "/api/v1/auth/change-password",
    "/api/v1/appearance/me",
}


def _fresh_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://sentinel.test")


async def _sign_in(client: AsyncClient, username: str, password: str) -> dict[str, object]:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    client.headers["Authorization"] = f"Bearer {body['access_token']}"
    return body


@pytest.fixture
async def viewer_account(admin_client: AsyncClient) -> dict[str, object]:
    created = await admin_client.post("/api/v1/auth/users", json=VIEWER)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["is_admin"] is False
    return body


@pytest.fixture
async def viewer_client(
    app: FastAPI, viewer_account: dict[str, object]
) -> AsyncIterator[AsyncClient]:
    async with _fresh_client(app) as client:
        await _sign_in(client, VIEWER["username"], VIEWER["password"])
        yield client


class TestManagingAccounts:
    async def test_an_administrator_adds_lists_and_removes_one(
        self, admin_client: AsyncClient, viewer_account: dict[str, object]
    ) -> None:
        listed = (await admin_client.get("/api/v1/auth/users")).json()
        assert [(user["username"], user["is_admin"]) for user in listed] == [
            (ADMIN_USERNAME, True),
            (VIEWER["username"], False),
        ]
        removed = await admin_client.delete(f"/api/v1/auth/users/{viewer_account['id']}")
        assert removed.status_code == 204
        listed = (await admin_client.get("/api/v1/auth/users")).json()
        assert [user["username"] for user in listed] == [ADMIN_USERNAME]

    async def test_a_taken_name_is_refused_whatever_its_case(
        self, admin_client: AsyncClient, viewer_account: dict[str, object]
    ) -> None:
        for username in (VIEWER["username"].upper(), ADMIN_USERNAME.upper()):
            response = await admin_client.post(
                "/api/v1/auth/users", json={"username": username, "password": "another-password-1"}
            )
            assert response.status_code == 409, username

    async def test_a_weak_password_is_refused(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/api/v1/auth/users", json={"username": "tom", "password": "1234567890"}
        )
        assert response.status_code == 422

    async def test_the_administrator_is_never_reset_or_removed_here(
        self, admin_client: AsyncClient
    ) -> None:
        admin_id = (await admin_client.get("/api/v1/auth/me")).json()["id"]
        reset = await admin_client.put(
            f"/api/v1/auth/users/{admin_id}/password", json={"password": "a-new-password-1"}
        )
        removed = await admin_client.delete(f"/api/v1/auth/users/{admin_id}")
        assert (reset.status_code, removed.status_code) == (409, 409)

    async def test_an_unknown_account_is_not_found(self, admin_client: AsyncClient) -> None:
        response = await admin_client.delete("/api/v1/auth/users/missing")
        assert response.status_code == 404

    async def test_a_reset_password_replaces_the_old_one(
        self, app: FastAPI, admin_client: AsyncClient, viewer_account: dict[str, object]
    ) -> None:
        reset = await admin_client.put(
            f"/api/v1/auth/users/{viewer_account['id']}/password",
            json={"password": "the-new-password-1"},
        )
        assert reset.status_code == 204
        async with _fresh_client(app) as client:
            old = await client.post("/api/v1/auth/login", json=VIEWER)
            assert old.status_code == 401
            await _sign_in(client, VIEWER["username"], "the-new-password-1")


class TestSigningIn:
    async def test_the_answer_says_which_kind_of_account_it_is(
        self, app: FastAPI, viewer_account: dict[str, object]
    ) -> None:
        async with _fresh_client(app) as client:
            assert (await _sign_in(client, VIEWER["username"], VIEWER["password"]))[
                "is_admin"
            ] is False
            assert (await _sign_in(client, ADMIN_USERNAME, ADMIN_PASSWORD))["is_admin"] is True

    async def test_the_account_sees_and_changes_only_itself(
        self, viewer_client: AsyncClient
    ) -> None:
        me = await viewer_client.get("/api/v1/auth/me")
        assert (me.status_code, me.json()["username"]) == (200, VIEWER["username"])
        changed = await viewer_client.post(
            "/api/v1/auth/change-password",
            json={"current_password": VIEWER["password"], "new_password": "my-own-password-1"},
        )
        assert changed.status_code == 204

    async def test_removing_the_account_signs_it_out_everywhere(
        self,
        admin_client: AsyncClient,
        viewer_client: AsyncClient,
        viewer_account: dict[str, object],
    ) -> None:
        assert (await viewer_client.get("/api/v1/viewer/cameras")).status_code == 200
        await admin_client.delete(f"/api/v1/auth/users/{viewer_account['id']}")
        assert (await viewer_client.get("/api/v1/viewer/cameras")).status_code == 401


class TestWatching:
    async def test_the_viewer_shows_the_cameras(
        self, admin_client: AsyncClient, viewer_client: AsyncClient
    ) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA)
        cameras = await viewer_client.get("/api/v1/viewer/cameras")
        assert cameras.status_code == 200, cameras.text
        assert [camera["name"] for camera in cameras.json()] == [CAMERA["name"]]
        assert (await viewer_client.get("/api/v1/viewer/system/status")).status_code == 200

    async def test_the_account_keeps_its_own_appearance(
        self, viewer_client: AsyncClient, viewer_account: dict[str, object]
    ) -> None:
        response = await viewer_client.get("/api/v1/viewer/appearance")
        assert response.status_code == 200
        assert response.json()["profile_id"] == viewer_account["id"]


class TestEverythingElse:
    async def test_every_other_route_treats_it_as_signed_out(
        self, app: FastAPI, admin_client: AsyncClient, viewer_client: AsyncClient
    ) -> None:
        """Same status as a client with no credentials, route by route."""
        await admin_client.post("/api/v1/cameras", json=CAMERA)
        routes = {
            path: {
                method
                for method in operations
                if method in {"get", "post", "put", "patch", "delete"}
            }
            for path, operations in app.openapi()["paths"].items()
        } | UNLISTED_ROUTES
        checked = 0
        async with _fresh_client(app) as anonymous:
            for template, methods in sorted(routes.items()):
                if template.startswith("/api/v1/viewer/"):
                    continue
                if template in VIEWER_STREAM_ROUTES or template in ACCOUNT_ROUTES:
                    continue
                path = re.sub(r"\{[^}]+\}", "missing", template)
                for method in sorted(methods):
                    body = {} if method in {"post", "put", "patch"} else None
                    expected = await anonymous.request(method, path, json=body)
                    actual = await viewer_client.request(method, path, json=body)
                    assert actual.status_code == expected.status_code, (method, template)
                    if expected.status_code < 400:
                        # Public by design (health, for example): what it says
                        # must not depend on who asked either. Values such as
                        # uptime move between two calls; the fields must not.
                        assert sorted(actual.json()) == sorted(expected.json()), (
                            method,
                            template,
                        )
                    checked += 1
        assert checked > 100, "the sweep must cover the whole API"

    async def test_the_administrators_live_socket_refuses_it(
        self, ws_server: str, admin_client: AsyncClient, viewer_client: AsyncClient
    ) -> None:
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA)).json()["id"]
        token = viewer_client.headers["Authorization"]
        with pytest.raises(InvalidStatus) as refused:
            async with websockets.connect(
                f"ws://{ws_server}/api/v1/streams/{camera_id}/ws",
                additional_headers={"Authorization": token},
            ):
                pass
        assert refused.value.response.status_code == 403

    async def test_the_viewer_socket_lets_it_watch(
        self, ws_server: str, admin_client: AsyncClient, viewer_client: AsyncClient
    ) -> None:
        """Past authorisation, the proxy closes with 1011 because no go2rtc runs
        in tests; a refused account would never get that far."""
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA)).json()["id"]
        token = viewer_client.headers["Authorization"]
        async with websockets.connect(
            f"ws://{ws_server}/api/v1/streams/{camera_id}/viewer-ws",
            additional_headers={"Authorization": token},
        ) as ws:
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=15)
        assert ws.close_code == 1011
