"""Motion, per camera and per named detection zone.

The binary sensors Sentinel genuinely drives in real time: a
``motion.started`` WebSocket frame flips the camera's sensor the instant the
detector confirms movement, and ``motion.zone_started`` does the same for each
named detection zone, not on the next poll. See :mod:`.coordinator` for how the
push path reaches ``coordinator.data`` before these entities ever ask.
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
    known_zones: set[tuple[str, str]] = set()

    @callback
    def _add_new_entities() -> None:
        new_ids = [
            camera_id
            for camera_id in coordinator.data.cameras
            if camera_id not in known
        ]
        new_zones = [
            (camera_id, zone.id)
            for camera_id, camera in coordinator.data.cameras.items()
            for zone in camera.motion_zones
            if (camera_id, zone.id) not in known_zones
        ]
        if not new_ids and not new_zones:
            return
        known.update(new_ids)
        known_zones.update(new_zones)
        entities: list[BinarySensorEntity] = [
            SentinelMotionSensor(coordinator, camera_id) for camera_id in new_ids
        ]
        entities.extend(
            SentinelZoneMotionSensor(coordinator, camera_id, zone_id)
            for camera_id, zone_id in new_zones
        )
        async_add_entities(entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


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


class SentinelZoneMotionSensor(SentinelEntity, BinarySensorEntity):
    """On while one named detection zone of a camera sees motion.

    Keyed on the zone's id, not its name, so renaming a zone in Sentinel keeps
    the entity, its history and every automation that uses it.
    """

    def __init__(
        self,
        coordinator: SentinelDataUpdateCoordinator,
        camera_id: str,
        zone_id: str,
    ) -> None:
        description = BinarySensorEntityDescription(
            key=f"zone_{zone_id}",
            device_class=BinarySensorDeviceClass.MOTION,
            translation_key="zone_motion",
        )
        super().__init__(coordinator, description, camera_id=camera_id)
        self._zone_id = zone_id
        self._attr_translation_placeholders = {"zone": self._zone_name()}

    def _zone_name(self) -> str:
        camera = self.camera
        zone = camera.zone(self._zone_id) if camera is not None else None
        return zone.name if zone is not None else self._zone_id

    @property
    def available(self) -> bool:
        """Unavailable once the zone is deleted in Sentinel."""
        camera = self.camera
        return (
            super().available
            and camera is not None
            and camera.zone(self._zone_id) is not None
        )

    @property
    def is_on(self) -> bool | None:
        if self._camera_id is None:
            return None
        return self.coordinator.data.zone_motion_for(self._camera_id, self._zone_id)
