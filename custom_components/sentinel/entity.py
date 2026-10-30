"""Shared entity base classes.

Two devices exist per config entry: the Sentinel server itself (system-wide
sensors) and one device per camera (everything else). Grouping camera entities
under their own device, rather than all under the server, is what lets Home
Assistant's device page show "this camera's motion sensor, this camera's
recording switch" together instead of one long undifferentiated entity list.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import SentinelDataUpdateCoordinator
from .models import CameraInfo


def server_device_info(coordinator: SentinelDataUpdateCoordinator) -> DeviceInfo:
    """The Sentinel server as its own device, distinct from any one camera."""
    server = coordinator.data.server
    return DeviceInfo(
        identifiers={(DOMAIN, server.unique_id)},
        name=server.name or "Sentinel CCTV",
        manufacturer=MANUFACTURER,
        model="Sentinel NVR",
        sw_version=server.version,
        configuration_url=coordinator.client.base_url,
    )


def camera_device_info(
    coordinator: SentinelDataUpdateCoordinator, camera_id: str
) -> DeviceInfo:
    """One device per camera, linked to the server device.

    Identified by the camera's own id rather than by name: Sentinel lets a
    camera be renamed, and a device identifier that changed with it would
    silently orphan every entity, automation and dashboard card pointing at it.
    """
    server = coordinator.data.server
    camera = coordinator.data.cameras.get(camera_id)
    name = camera.name if camera is not None else camera_id
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{server.unique_id}:{camera_id}")},
        name=name,
        manufacturer=(camera.manufacturer if camera is not None else None)
        or MANUFACTURER,
        model=(camera.model if camera is not None else None) or "Camera",
    )
    # Home Assistant 2026.10 refuses to add a device given `via_device`; older
    # releases do not know `via_device_id`.
    if "via_device_id" in DeviceInfo.__optional_keys__:
        if coordinator.server_device_id is not None:
            info["via_device_id"] = coordinator.server_device_id
    else:
        info["via_device"] = (DOMAIN, server.unique_id)
    return info


class SentinelEntity(CoordinatorEntity[SentinelDataUpdateCoordinator]):
    """Base for every entity backed by the coordinator.

    ``_attr_has_entity_name`` plus a device-scoped unique id is what lets Home
    Assistant compose "Front door Motion" from "Front door" (the device name)
    and "Motion" (this entity's own name), rather than every entity spelling
    out the camera name itself.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SentinelDataUpdateCoordinator,
        description: EntityDescription,
        *,
        camera_id: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._camera_id = camera_id
        server_unique_id = coordinator.data.server.unique_id
        if camera_id is None:
            self._attr_unique_id = f"{server_unique_id}_{description.key}"
            self._attr_device_info = server_device_info(coordinator)
        else:
            self._attr_unique_id = f"{server_unique_id}_{camera_id}_{description.key}"
            self._attr_device_info = camera_device_info(coordinator, camera_id)

    @property
    def camera(self) -> CameraInfo | None:
        """The camera this entity describes, or ``None`` if it was removed."""
        if self._camera_id is None:
            return None
        return self.coordinator.data.cameras.get(self._camera_id)

    @property
    def available(self) -> bool:
        """Unavailable once the coordinator itself gives up, or the camera is gone.

        A camera that Sentinel reports as offline is still *available* as an
        entity -- offline is a meaningful state to show, not an excuse to grey
        the entity out. Only a camera removed from Sentinel entirely, or a
        coordinator that has stopped getting any answer at all, counts.
        """
        if not super().available:
            return False
        return (
            self._camera_id is None or self._camera_id in self.coordinator.data.cameras
        )
