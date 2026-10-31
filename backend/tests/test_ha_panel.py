"""The Home Assistant panel link created from the administration."""

from __future__ import annotations

from httpx import AsyncClient
from pydantic import SecretStr

from app.config import Settings
from app.viewer.auth import VIEWER_COOKIE_NAME

PANEL = "/api/v1/integrations/home-assistant/panel"


def _sign_out(client: AsyncClient) -> None:
    """The admin fixture signs in with a header as well as the cookie."""
    client.headers.pop("Authorization", None)
    client.cookies.clear()


async def _open_viewer(client: AsyncClient, key: str) -> int:
    client.cookies.delete(VIEWER_COOKIE_NAME)
    response = await client.post(
        "/api/v1/viewer/session",
        content=key,
        headers={"content-type": "text/plain;charset=UTF-8"},
    )
    return response.status_code


class TestPanelLink:
    async def test_a_created_link_opens_the_viewer_without_the_env_switch(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        assert settings.viewer_enabled is False
        key = (await admin_client.post(f"{PANEL}/link")).json()["key"]
        _sign_out(admin_client)

        assert await _open_viewer(admin_client, key) == 204
        # The cookie carries the key that was supplied, so it keeps working.
        assert admin_client.cookies.get(VIEWER_COOKIE_NAME) == key
        assert (await admin_client.get("/api/v1/viewer/cameras")).status_code == 200
        assert (await admin_client.get(PANEL)).status_code == 401

    async def test_a_new_link_revokes_the_previous_one(self, admin_client: AsyncClient) -> None:
        first = (await admin_client.post(f"{PANEL}/link")).json()["key"]
        second = (await admin_client.post(f"{PANEL}/link")).json()["key"]
        assert first != second
        _sign_out(admin_client)

        assert await _open_viewer(admin_client, second) == 204
        assert await _open_viewer(admin_client, first) == 401

    async def test_env_key_still_works_after_a_panel_link_exists(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("e" * 48)
        await admin_client.post(f"{PANEL}/link")
        status = (await admin_client.get(PANEL)).json()
        assert status == {"link_exists": True, "env_key": True, "origins": []}
        _sign_out(admin_client)

        assert await _open_viewer(admin_client, "e" * 48) == 204

    async def test_the_key_is_stored_only_as_a_hash(self, admin_client: AsyncClient) -> None:
        key = (await admin_client.post(f"{PANEL}/link")).json()["key"]
        backup = await admin_client.get("/api/v1/system/diagnostics")
        assert key not in backup.text


class TestPanelOrigins:
    async def test_origins_are_added_to_frame_ancestors(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        settings.frame_ancestors = ["https://env.example.net"]
        response = await admin_client.put(
            f"{PANEL}/origins",
            json={"origins": ["https://ha.example.net/", "http://192.168.50.10:8123"]},
        )
        assert response.json()["origins"] == ["https://ha.example.net", "http://192.168.50.10:8123"]

        health = await admin_client.get("/api/v1/system/health")
        policy = health.headers["content-security-policy"]
        assert (
            "frame-ancestors 'self' https://env.example.net https://ha.example.net "
            "http://192.168.50.10:8123" in policy
        )

    async def test_a_public_http_origin_is_refused(self, admin_client: AsyncClient) -> None:
        response = await admin_client.put(
            f"{PANEL}/origins", json={"origins": ["http://ha.example.net"]}
        )
        assert response.status_code == 422


class TestOnlyTheAdministrator:
    async def test_only_the_administrator_reads_or_changes_the_panel(
        self, admin_client: AsyncClient, client: AsyncClient
    ) -> None:
        created = await admin_client.post("/api/v1/auth/tokens", json={"name": "ha"})
        token = created.json()["token"]
        _sign_out(admin_client)
        bearer = {"Authorization": f"Bearer {token}"}

        assert (await client.get(PANEL, headers=bearer)).status_code == 403
        assert (await client.post(f"{PANEL}/link", headers=bearer)).status_code == 403
        assert (
            await client.put(f"{PANEL}/origins", json={"origins": []}, headers=bearer)
        ).status_code == 403
        assert (await client.post(f"{PANEL}/link")).status_code == 401
