"""Tapo battery cameras are developed on the dev build, not shipped in releases.

A release refuses to add one. A camera that the dev build left in the database
stays there untouched -- listed, deletable and otherwise inert -- so going back
to the dev build finds it as it was.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.cameras.service import BATTERY_UNSUPPORTED
from app.database.models import Camera, CameraKind, RecordingMode
from app.database.session import session_scope


async def _left_by_the_dev_build() -> str:
    """A battery camera stored the way the dev build stores one."""
    async with session_scope() as session:
        camera = Camera(
            name="Patio",
            kind=CameraKind.TAPO_BATTERY,
            host="192.168.50.220",
            username="admin",
            manufacturer="Tapo",
            model="TC82",
            recording_mode=RecordingMode.EVENTS,
            motion_enabled=False,
        )
        session.add(camera)
        await session.flush()
        return camera.id


async def test_a_release_refuses_to_add_one(admin_client: AsyncClient) -> None:
    response = await admin_client.post(
        "/api/v1/cameras",
        json={
            "name": "Patio",
            "kind": "tapo_battery",
            "host": "192.168.50.220",
            "tapo_password": "local-secret",
        },
    )
    assert response.status_code == 422
    assert BATTERY_UNSUPPORTED in response.text
    assert (await admin_client.get("/api/v1/cameras")).json() == []


async def test_one_left_by_the_dev_build_is_still_listed(admin_client: AsyncClient) -> None:
    camera_id = await _left_by_the_dev_build()
    cameras = (await admin_client.get("/api/v1/cameras")).json()
    assert [(camera["id"], camera["kind"]) for camera in cameras] == [(camera_id, "tapo_battery")]
    assert cameras[0]["tapo_controls"]["supported"] is False


async def test_one_left_by_the_dev_build_cannot_be_changed(admin_client: AsyncClient) -> None:
    camera_id = await _left_by_the_dev_build()
    response = await admin_client.patch(f"/api/v1/cameras/{camera_id}", json={"name": "Greenhouse"})
    assert response.status_code == 422
    assert response.json()["detail"] == BATTERY_UNSUPPORTED
    assert (await admin_client.get(f"/api/v1/cameras/{camera_id}")).json()["name"] == "Patio"


async def test_one_left_by_the_dev_build_can_be_deleted(admin_client: AsyncClient) -> None:
    camera_id = await _left_by_the_dev_build()
    assert (await admin_client.delete(f"/api/v1/cameras/{camera_id}")).status_code == 204
    assert (await admin_client.get(f"/api/v1/cameras/{camera_id}")).status_code == 404


async def test_it_has_no_recorder_to_toggle(admin_client: AsyncClient) -> None:
    camera_id = await _left_by_the_dev_build()
    response = await admin_client.put(
        f"/api/v1/cameras/{camera_id}/recording", json={"enabled": False, "confirm": True}
    )
    assert response.status_code == 409


async def test_a_release_never_contacts_it_for_tapo_controls(admin_client: AsyncClient) -> None:
    camera_id = await _left_by_the_dev_build()
    response = await admin_client.get(f"/api/v1/cameras/{camera_id}/tapo/capabilities")
    assert response.status_code == 409
    assert response.json()["detail"] == "Tapo battery cameras are not supported in this release."
