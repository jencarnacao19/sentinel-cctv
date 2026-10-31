"""Sentinel's recipes become Home Assistant automations, within the agreed limits."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import dump, load_yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.sentinel.automation_sync import async_sync_automations
from custom_components.sentinel.automations import Entities, build_automation, refusal
from custom_components.sentinel.const import (
    CONF_ALLOW_AUTOMATIONS,
    CONF_TOKEN,
    CONF_URL,
    DOMAIN,
)

from .conftest import BASE_URL, SERVER_ID, TOKEN, register_sentinel

RECIPE: dict[str, Any] = {
    "id": "a1b2c3d4",
    "kind": "motion_alert",
    "name": "Door",
    "camera_id": "cam-1",
    "zone_id": None,
    "notify": ["mobile_app_phone"],
    "when": "always",
    "image": True,
    "cooldown_minutes": 5,
    "reclaim": 0,
}
OWN = {
    "id": "1700000000000",
    "alias": "My own automation",
    "triggers": [{"trigger": "sun", "event": "sunset"}],
    "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}],
}
STATUS_URL = "/integrations/home-assistant/automations/status"


def _file(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("automations.yaml"))


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    *,
    allow: bool,
    recipes: list[dict[str, Any]],
) -> MockConfigEntry:
    aioclient_mock.clear_requests()
    register_sentinel(aioclient_mock, recipes=recipes)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERVER_ID,
        data={CONF_URL: BASE_URL, CONF_TOKEN: TOKEN},
        options={CONF_ALLOW_AUTOMATIONS: allow},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def _last_status(aioclient_mock: AiohttpClientMocker) -> dict[str, Any]:
    calls = [
        call for call in aioclient_mock.mock_calls if str(call[1]).endswith(STATUS_URL)
    ]
    return calls[-1][2]["status"]


async def test_nothing_is_written_without_permission(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _file(hass).write_text(dump([OWN]))
    before = _file(hass).read_text()
    await _setup(hass, aioclient_mock, allow=False, recipes=[RECIPE])

    assert _file(hass).read_text() == before
    assert _last_status(aioclient_mock)["a1b2c3d4"]["state"] == "waiting_permission"


async def test_sync_keeps_the_users_automations(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _file(hass).write_text(dump([OWN]))
    await _setup(hass, aioclient_mock, allow=True, recipes=[RECIPE])

    automations = load_yaml(str(_file(hass)))
    assert automations[0] == OWN
    ours = automations[1]
    assert ours["id"] == "sentinel_a1b2c3d4"
    assert ours["alias"] == "Sentinel: Door"
    assert ours["actions"][0]["action"] == "notify.mobile_app_phone"
    assert _file(hass).with_name("automations.yaml.before-sentinel").exists()
    assert _last_status(aioclient_mock)["a1b2c3d4"]["state"] == "created"


async def test_an_edited_automation_is_left_alone_until_given_back(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _file(hass).write_text(dump([]))
    entry = await _setup(hass, aioclient_mock, allow=True, recipes=[RECIPE])
    automations = load_yaml(str(_file(hass)))
    automations[0]["alias"] = "Front door, my way"
    _file(hass).write_text(dump(automations))

    await async_sync_automations(hass, entry, entry.runtime_data)
    assert load_yaml(str(_file(hass)))[0]["alias"] == "Front door, my way"
    assert _last_status(aioclient_mock)["a1b2c3d4"]["state"] == "edited_in_ha"

    aioclient_mock.clear_requests()
    register_sentinel(aioclient_mock, recipes=[{**RECIPE, "reclaim": 1}])
    await async_sync_automations(hass, entry, entry.runtime_data)
    assert load_yaml(str(_file(hass)))[0]["alias"] == "Sentinel: Door"


async def test_a_missing_entity_refuses_only_that_recipe(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _file(hass).write_text(dump([]))
    gone = {**RECIPE, "id": "b1b2c3d4", "camera_id": "deleted-camera"}
    await _setup(hass, aioclient_mock, allow=True, recipes=[RECIPE, gone])

    status = _last_status(aioclient_mock)
    assert status["a1b2c3d4"]["state"] == "created"
    assert status["b1b2c3d4"]["state"] == "refused"
    assert [item["id"] for item in load_yaml(str(_file(hass)))] == ["sentinel_a1b2c3d4"]


ENTITIES = Entities(
    camera_name="Front door",
    motion="binary_sensor.front_door_motion",
    camera="camera.front_door",
    own_switches=frozenset({"switch.front_door_recording"}),
)


def test_motion_alert_waits_out_its_cooldown() -> None:
    config = build_automation(RECIPE, ENTITIES)
    assert config["mode"] == "single"
    assert config["actions"][-1] == {"delay": {"minutes": 5}}
    assert config["actions"][0]["data"]["data"] == {
        "image": "/api/camera_proxy/camera.front_door"
    }


def test_forbidden_actions_are_refused() -> None:
    own = ENTITIES.own_switches
    for step in (
        {"action": "lock.unlock", "target": {"entity_id": "lock.front"}},
        {"action": "cover.open_cover", "target": {"entity_id": "cover.garage"}},
        {
            "action": "alarm_control_panel.alarm_disarm",
            "target": {"entity_id": "alarm_control_panel.a"},
        },
        {
            "action": "alarm_control_panel.alarm_trigger",
            "target": {"entity_id": "alarm_control_panel.a"},
        },
        {"action": "switch.turn_on", "target": {"entity_id": "switch.gate"}},
        {"action": "script.anything"},
        {"event": "anything"},
    ):
        assert refusal({"actions": [step]}, own) is not None, step
    guarded = {
        "if": [
            {
                "condition": "state",
                "entity_id": "alarm_control_panel.a",
                "state": ["armed_away"],
            }
        ],
        "then": [
            {
                "action": "alarm_control_panel.alarm_trigger",
                "target": {"entity_id": "alarm_control_panel.a"},
            }
        ],
    }
    assert refusal({"actions": [guarded]}, own) is None
