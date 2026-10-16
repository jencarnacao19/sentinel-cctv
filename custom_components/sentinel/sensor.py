"""Storage figures for the server, and per-camera state.

Deliberately few entities. A camera could easily produce a dozen sensors --
codec, resolution, frame rate, bitrate, every timestamp the API returns -- and
most would be a value nobody automates on, cluttering the entity list and the
recorder database for ever. What is here is what a person actually builds an
automation or a dashboard card from.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SentinelData, SentinelDataUpdateCoordinator
from .entity import SentinelEntity
from .models import RECORDING_STATES, CameraInfo, derive_recording_state


@dataclass(frozen=True, kw_only=True)
class SentinelServerSensorDescription(SensorEntityDescription):
    """A sensor reading one figure out of the coordinator's server-wide data."""

    value_fn: Callable[[SentinelData], float | int | str | None]


@dataclass(frozen=True, kw_only=True)
class SentinelCameraSensorDescription(SensorEntityDescription):
    """A sensor reading one figure off a single camera."""

    value_fn: Callable[[CameraInfo, SentinelData], float | int | str | None]


SERVER_SENSORS: tuple[SentinelServerSensorDescription, ...] = (
    SentinelServerSensorDescription(
        key="storage_used",
        translation_key="storage_used",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        # Unknown rather than zero when the volume has gone: a graph that drops
        # to zero reads as "the footage was deleted", which is a very different
        # emergency from "the disk is unreachable".
        value_fn=lambda data: (
            data.storage.used_bytes if data.storage.available else None
        ),
    ),
    SentinelServerSensorDescription(
        key="storage_free",
        translation_key="storage_free",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.storage.free_bytes if data.storage.available else None
        ),
    ),
    SentinelServerSensorDescription(
        key="recordings_size",
        translation_key="recordings_size",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.storage.recordings_bytes if data.storage.available else None
        ),
    ),
    SentinelServerSensorDescription(
        key="retention_days",
        translation_key="retention_days",
        native_unit_of_measurement=UnitOfTime.DAYS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.retention_days,
    ),
    SentinelServerSensorDescription(
        key="cameras_online",
        translation_key="cameras_online",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: sum(
            1 for camera in data.cameras.values() if camera.status == "online"
        ),
    ),
)


CAMERA_SENSORS: tuple[SentinelCameraSensorDescription, ...] = (
    SentinelCameraSensorDescription(
        key="recording_state",
        translation_key="recording_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(RECORDING_STATES),
        value_fn=lambda camera, data: derive_recording_state(
            camera, motion_active=data.motion_for(camera.id)
        ),
    ),
    SentinelCameraSensorDescription(
        key="bitrate",
        translation_key="bitrate",
        native_unit_of_measurement="kbit/s",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda camera, _data: camera.stream.bitrate_kbps,
    ),
    SentinelCameraSensorDescription(
        key="motion_events_total",
        translation_key="motion_events_total",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda camera, data: data.event_counts.get(camera.id),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SentinelDataUpdateCoordinator = entry.runtime_data

    async_add_entities(
        SentinelServerSensor(coordinator, description) for description in SERVER_SENSORS
    )

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
            SentinelCameraSensor(coordinator, description, camera_id)
            for camera_id in new_ids
            for description in CAMERA_SENSORS
        )

    _add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_cameras))


class SentinelServerSensor(SentinelEntity, SensorEntity):
    """One figure about the Sentinel install as a whole."""

    entity_description: SentinelServerSensorDescription

    def __init__(
        self,
        coordinator: SentinelDataUpdateCoordinator,
        description: SentinelServerSensorDescription,
    ) -> None:
        super().__init__(coordinator, description)

    @property
    def native_value(self) -> float | int | str | None:
        return self.entity_description.value_fn(self.coordinator.data)


class SentinelCameraSensor(SentinelEntity, SensorEntity):
    """One figure about a single camera."""

    entity_description: SentinelCameraSensorDescription

    def __init__(
        self,
        coordinator: SentinelDataUpdateCoordinator,
        description: SentinelCameraSensorDescription,
        camera_id: str,
    ) -> None:
        super().__init__(coordinator, description, camera_id=camera_id)

    @property
    def native_value(self) -> float | int | str | None:
        camera = self.camera
        if camera is None:
            return None
        return self.entity_description.value_fn(camera, self.coordinator.data)
