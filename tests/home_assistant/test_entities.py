"""Entity states must match the server contract, not optimistic fixtures."""

from __future__ import annotations

from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sentinel.const import DOMAIN
from custom_components.sentinel.models import (
    RECORDING_STATES,
    CameraInfo,
    derive_recording_state,
)

from .conftest import SERVER_ID


def test_recording_states_match_every_value_the_contract_can_return() -> None:
    assert RECORDING_STATES == ("recording", "idle", "off", "unavailable")
    assert (
        derive_recording_state(CameraInfo(id="one", name="One", enabled=False)) == "off"
    )
    assert (
        derive_recording_state(
            CameraInfo(
                id="two", name="Two", status="offline", recording_mode="continuous"
            )
        )
        == "unavailable"
    )


async def test_loaded_entities_use_honest_stream_and_count_metadata(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    registry = er.async_get(hass)

    camera_entity_id = registry.async_get_entity_id(
        "camera", DOMAIN, f"{SERVER_ID}_cam-1_camera"
    )
    assert camera_entity_id is not None
    camera_state = hass.states.get(camera_entity_id)
    assert camera_state is not None
    assert camera_state.attributes[ATTR_SUPPORTED_FEATURES] == 0

    total_entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{SERVER_ID}_cam-1_motion_events_total"
    )
    assert total_entity_id is not None
    total_state = hass.states.get(total_entity_id)
    assert total_state is not None
    assert total_state.state == "7"

    recording_entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{SERVER_ID}_cam-1_recording_state"
    )
    assert recording_entity_id is not None
    recording_state = hass.states.get(recording_entity_id)
    assert recording_state is not None
    assert recording_state.state == "recording"
    assert recording_state.attributes["options"] == list(RECORDING_STATES)
