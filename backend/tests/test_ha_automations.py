"""Automation recipes for Home Assistant, saved in the administration."""

from __future__ import annotations

from httpx import AsyncClient

from app.events.bus import get_bus

BASE = "/api/v1/integrations/home-assistant"
RECIPE = {
    "id": "a1b2c3d4",
    "kind": "motion_alert",
    "name": "Gate at night",
    "camera_id": "cam-1",
    "zone_id": "z1",
    "notify": ["mobile_app_phone"],
    "when": "night",
}


async def _token(admin_client: AsyncClient) -> dict[str, str]:
    created = await admin_client.post(
        "/api/v1/auth/tokens", json={"name": "ha", "scope": "camera_control"}
    )
    return {"Authorization": f"Bearer {created.json()['token']}"}


async def test_the_administrator_saves_recipes_and_the_integration_reads_them(
    admin_client: AsyncClient,
) -> None:
    saved = await admin_client.put(f"{BASE}/automations", json={"recipes": [RECIPE]})
    assert saved.status_code == 200
    recipe = saved.json()["recipes"][0]
    assert recipe["cooldown_minutes"] == 5 and recipe["image"] is True

    bearer = await _token(admin_client)
    admin_client.headers.pop("Authorization", None)
    admin_client.cookies.clear()
    read = await admin_client.get(f"{BASE}/automations", headers=bearer)
    assert read.json()["recipes"][0]["id"] == "a1b2c3d4"


async def test_saving_tells_the_integration_to_sync(admin_client: AsyncClient) -> None:
    before = get_bus().sequence
    await admin_client.put(f"{BASE}/automations", json={"recipes": [RECIPE]})
    names = [event.name for event in get_bus().replay_since(before) or []]
    assert names == ["automations.changed"]


async def test_bad_recipes_are_refused(admin_client: AsyncClient) -> None:
    for bad in (
        {**RECIPE, "lights": ["lock.front_door"]},
        {**RECIPE, "notify": ["mobile app; rm"]},
        {**RECIPE, "alarm_entity": "lock.front"},
        {**RECIPE, "kind": "open_gate"},
        {**RECIPE, "id": "../x"},
    ):
        response = await admin_client.put(f"{BASE}/automations", json={"recipes": [bad]})
        assert response.status_code == 422, bad
    twice = await admin_client.put(f"{BASE}/automations", json={"recipes": [RECIPE, RECIPE]})
    assert twice.status_code == 422


async def test_the_integration_reports_targets_and_status(admin_client: AsyncClient) -> None:
    bearer = await _token(admin_client)
    targets = {
        "allowed": True,
        "automations_file": True,
        "notify": [{"id": "mobile_app_phone", "name": "Phone"}],
        "lights": [{"id": "light.porch", "name": "Porch"}],
        "people": [],
        "alarms": [],
        "reported_at": "2026-10-31T12:00:00Z",
    }
    assert (
        await admin_client.put(f"{BASE}/targets", json=targets, headers=bearer)
    ).status_code == 204
    status = {"status": {"a1b2c3d4": {"state": "edited_in_ha", "message": ""}}}
    assert (
        await admin_client.put(f"{BASE}/automations/status", json=status, headers=bearer)
    ).status_code == 204

    state = (await admin_client.get(f"{BASE}/automations")).json()
    assert state["targets"]["lights"] == [{"id": "light.porch", "name": "Porch"}]
    assert state["status"]["a1b2c3d4"]["state"] == "edited_in_ha"


async def test_only_the_integration_reports_and_only_the_administrator_saves(
    admin_client: AsyncClient,
) -> None:
    # An administrator session cannot pretend to be Home Assistant.
    assert (await admin_client.put(f"{BASE}/targets", json={})).status_code == 403
    bearer = await _token(admin_client)
    admin_client.headers.pop("Authorization", None)
    admin_client.cookies.clear()
    response = await admin_client.put(
        f"{BASE}/automations", json={"recipes": [RECIPE]}, headers=bearer
    )
    assert response.status_code == 403
