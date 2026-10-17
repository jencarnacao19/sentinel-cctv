"""One motion sensor per named detection zone, driven by push events."""

from __future__ import annotations

import copy
from typing import Any

from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.sentinel.const import DOMAIN, EVENT_ZONE_MOTION_STARTED
from custom_components.sentinel.models import CameraInfo, SentinelEvent

from .conftest import CAMERA_PAYLOAD, HANDSHAKE_PAYLOAD, SERVER_ID, register_sentinel

ZONE_UNIQUE_ID = f"{SERVER_ID}_cam-1_zone_gate"


def _handshake(zones: list[dict[str, Any]]) -> dict[str, Any]:
    handshake = copy.deepcopy(HANDSHAKE_PAYLOAD)
    handshake["cameras"][0]["motion_zones"] = zones
    return handshake


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    zones: list[dict[str, Any]],
) -> None:
    register_sentinel(aioclient_mock, handshake=_handshake(zones))
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


def _zone_entity_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, ZONE_UNIQUE_ID
    )
    assert entity_id is not None
    return entity_id


def test_zones_are_read_from_the_handshake() -> None:
    camera = CameraInfo.from_payload(
        {
            **CAMERA_PAYLOAD,
            "motion_zones": [
                {"id": "gate", "name": "Gate", "active": True},
                {"name": "no id"},
                "nonsense",
            ],
        }
    )
    assert [(zone.id, zone.name, zone.active) for zone in camera.motion_zones] == [
        ("gate", "Gate", True)
    ]
    assert camera.zone("gate") is not None
    assert camera.zone("missing") is None


def test_a_server_without_zones_still_parses() -> None:
    assert CameraInfo.from_payload(CAMERA_PAYLOAD).motion_zones == ()


async def test_each_detection_zone_gets_a_motion_sensor(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    await _setup(
        hass,
        aioclient_mock,
        config_entry,
        [{"id": "gate", "name": "Gate", "active": False}],
    )
    state = hass.states.get(_zone_entity_id(hass))
    assert state is not None
    assert state.state == "off"
    assert state.attributes["device_class"] == "motion"
    assert state.attributes["friendly_name"] == "Front door Gate motion"


async def test_a_zone_event_switches_its_sensor_and_fires_an_event(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    await _setup(
        hass,
        aioclient_mock,
        config_entry,
        [{"id": "gate", "name": "Gate", "active": False}],
    )
    fired: list[Event] = []
    hass.bus.async_listen(EVENT_ZONE_MOTION_STARTED, fired.append)

    coordinator = config_entry.runtime_data
    coordinator._handle_event(
        SentinelEvent.from_payload(
            {
                "type": "motion.zone_started",
                "seq": 1,
                "data": {
                    "camera_id": "cam-1",
                    "camera": "Front door",
                    "zone_id": "gate",
                    "zone": "Gate",
                    "event_id": "event-1",
                },
            }
        )
    )
    await hass.async_block_till_done()

    state = hass.states.get(_zone_entity_id(hass))
    assert state is not None
    assert state.state == "on"
    assert len(fired) == 1
    assert fired[0].data["zone_id"] == "gate"
    assert fired[0].data["zone_name"] == "Gate"

    coordinator._handle_event(
        SentinelEvent.from_payload(
            {
                "type": "motion.zone_ended",
                "seq": 2,
                "data": {"camera_id": "cam-1", "zone_id": "gate", "zone": "Gate"},
            }
        )
    )
    await hass.async_block_till_done()
    state = hass.states.get(_zone_entity_id(hass))
    assert state is not None
    assert state.state == "off"


async def test_a_deleted_zone_becomes_unavailable(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    await _setup(
        hass,
        aioclient_mock,
        config_entry,
        [{"id": "gate", "name": "Gate", "active": False}],
    )
    entity_id = _zone_entity_id(hass)

    aioclient_mock.clear_requests()
    register_sentinel(aioclient_mock, handshake=_handshake([]))
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "unavailable"
