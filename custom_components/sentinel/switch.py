"""Recording enabled, per camera.

Sentinel's ``recording_mode`` has four values, but as of this Sentinel phase
only ``continuous`` actually starts a recorder -- ``motion`` and ``scheduled``
are accepted and stored but record nothing (see the NVR's own
``PROJECT_STATUS.md``). A three- or four-state selector would therefore be
lying about two of its options doing anything, so this is deliberately a
switch: on means ``continuous``, off means ``off``, and a camera sitting in
``motion`` or ``scheduled`` reads as off, because that is what it is actually
doing right now. Turning it on replaces whatever mode was set with the one
mode that works.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import SentinelError
from .coordinator import SentinelDataUpdateCoordinator
from .entity import SentinelEntity

_LOGGER = logging.getLogger(__name__)

_MODE_CONTINUOUS = "continuous"
_MODE_OFF = "off"

RECORDING_DESCRIPTION = SwitchEntityDescription(
    key="recording",
    translation_key="recording",
    entity_registry_enabled_default=True,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SentinelDataUpdateCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_cameras() -> None:
        new_ids = [
            camera_id
            for camera_id in coordinator.data.cameras
            if camera_id not in known
        ]
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(
            SentinelRecordingSwitch(coordinator, camera_id) for camera_id in new_ids
        )

    _add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_cameras))


class SentinelRecordingSwitch(SentinelEntity, SwitchEntity):
    """Toggles a camera between continuous recording and off."""

    def __init__(
        self, coordinator: SentinelDataUpdateCoordinator, camera_id: str
    ) -> None:
        super().__init__(coordinator, RECORDING_DESCRIPTION, camera_id=camera_id)

    @property
    def is_on(self) -> bool | None:
        camera = self.camera
        if camera is None or camera.recording_mode is None:
            return None
        return camera.recording_mode == _MODE_CONTINUOUS

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_set_mode(_MODE_CONTINUOUS)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_set_mode(_MODE_OFF)

    async def _async_set_mode(self, mode: str) -> None:
        if self._camera_id is None:
            return
        try:
            await self.coordinator.client.async_set_recording_mode(
                self._camera_id, mode
            )
        except SentinelError as err:
            raise HomeAssistantError(
                f"Sentinel would not change the recording mode: {err}"
            ) from err
        await self.coordinator.async_request_refresh()
