"""The administrator's arrangement of the camera wall."""

from __future__ import annotations

from httpx import AsyncClient
from pydantic import SecretStr

from app.config import Settings

LAYOUT = {"columns": "2x2", "order": ["b", "a"], "hidden": ["c"]}


async def test_the_wall_starts_automatic_with_every_camera(admin_client: AsyncClient) -> None:
    body = (await admin_client.get("/api/v1/viewer/wall")).json()
    assert body == {"columns": "auto", "order": [], "hidden": []}


async def test_the_administrator_saves_it_and_every_viewer_reads_it(
    admin_client: AsyncClient, settings: Settings
) -> None:
    saved = await admin_client.put("/api/v1/wall", json=LAYOUT)
    assert saved.status_code == 200
    assert (await admin_client.get("/api/v1/viewer/wall")).json() == LAYOUT

    settings.viewer_enabled = True
    settings.viewer_key = SecretStr("w" * 48)
    admin_client.headers.pop("Authorization", None)
    admin_client.cookies.clear()
    viewer = await admin_client.get(
        "/api/v1/viewer/wall", headers={"X-Sentinel-Viewer-Key": "w" * 48}
    )
    assert viewer.json() == LAYOUT


async def test_only_the_administrator_arranges_it(
    admin_client: AsyncClient, settings: Settings
) -> None:
    settings.viewer_enabled = True
    settings.viewer_key = SecretStr("w" * 48)
    admin_client.headers.pop("Authorization", None)
    admin_client.cookies.clear()
    response = await admin_client.put(
        "/api/v1/wall", json=LAYOUT, headers={"X-Sentinel-Viewer-Key": "w" * 48}
    )
    assert response.status_code == 401
    assert (await admin_client.get("/api/v1/viewer/wall")).status_code == 401


async def test_an_unknown_grid_is_refused(admin_client: AsyncClient) -> None:
    response = await admin_client.put("/api/v1/wall", json={**LAYOUT, "columns": "5x5"})
    assert response.status_code == 422
