"""Switching between the stable and dev builds from the administration."""

from __future__ import annotations

import json
import stat

from httpx import AsyncClient

from app.config import Settings

CHANNEL = "/api/v1/system/channel"


async def test_it_reports_the_build_and_a_missing_helper(
    admin_client: AsyncClient, settings: Settings
) -> None:
    body = (await admin_client.get(CHANNEL)).json()
    assert body == {
        "current": "stable",
        "requested": None,
        "last_result": None,
        "helper_seen_at": None,
    }


async def test_channel_change_needs_the_typed_confirmation(admin_client: AsyncClient) -> None:
    for confirm in ("", "dev", "yes", "STABLE"):
        response = await admin_client.post(CHANNEL, json={"channel": "dev", "confirm": confirm})
        assert response.status_code == 422


async def test_a_confirmed_request_is_written_for_the_helper(
    admin_client: AsyncClient, settings: Settings
) -> None:
    response = await admin_client.post(CHANNEL, json={"channel": "dev", "confirm": "DEV"})
    assert response.status_code == 202
    assert response.json()["requested"] == "dev"

    request = settings.data_dir / "update-request.json"
    assert json.loads(request.read_text())["channel"] == "dev"
    assert stat.S_IMODE(request.stat().st_mode) & 0o044 == 0o044


async def test_the_running_build_cannot_be_requested_again(admin_client: AsyncClient) -> None:
    response = await admin_client.post(CHANNEL, json={"channel": "stable", "confirm": "STABLE"})
    assert response.status_code == 409


async def test_the_helper_reports_are_shown(admin_client: AsyncClient, settings: Settings) -> None:
    (settings.data_dir / "update-helper.json").write_text('{"seen_at": "2026-10-31T10:00:00Z"}')
    (settings.data_dir / "update-status.json").write_text(
        '{"channel": "dev", "state": "failed", "message": "pull failed"}'
    )
    body = (await admin_client.get(CHANNEL)).json()
    assert body["helper_seen_at"] == "2026-10-31T10:00:00Z"
    assert body["last_result"]["state"] == "failed"


async def test_only_the_administrator_can_switch(
    admin_client: AsyncClient, client: AsyncClient
) -> None:
    created = await admin_client.post("/api/v1/auth/tokens", json={"name": "ha"})
    bearer = {"Authorization": f"Bearer {created.json()['token']}"}
    client.headers.pop("Authorization", None)
    client.cookies.clear()

    assert (await client.get(CHANNEL, headers=bearer)).status_code == 403
    response = await client.post(CHANNEL, json={"channel": "dev", "confirm": "DEV"}, headers=bearer)
    assert response.status_code == 403
    assert (await client.get(CHANNEL)).status_code == 401
