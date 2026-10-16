"""Motion, per camera.

The one binary sensor Sentinel genuinely drives in real time: a
``motion.started`` WebSocket frame flips it the instant the detector confirms
movement, not on the next poll. See :mod:`.coordinator` for how the push path
reaches ``coordinator.data`` before this entity ever asks.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SentinelDataUpdateCoordinator
from .entity import SentinelEntity

MOTION_DESCRIPTION = BinarySensorEntityDescription(
    key="motion",
    device_class=BinarySensorDeviceClass.MOTION,
    translation_key="motion",
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
            SentinelMotionSensor(coordinator, camera_id) for camera_id in new_ids
        )

    _add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_cameras))


class SentinelMotionSensor(SentinelEntity, BinarySensorEntity):
    """On for as long as Sentinel has an open motion event for this camera."""

    def __init__(
        self, coordinator: SentinelDataUpdateCoordinator, camera_id: str
    ) -> None:
        super().__init__(coordinator, MOTION_DESCRIPTION, camera_id=camera_id)

    @property
    def is_on(self) -> bool | None:
        if self._camera_id is None:
            return None
        return self.coordinator.data.motion_for(self._camera_id)
