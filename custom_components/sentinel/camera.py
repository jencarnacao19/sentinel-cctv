"""One camera entity per Sentinel camera.

Snapshots always come from Sentinel, never from the camera. A live-stream
feature is advertised only if a future/server-compatible handshake supplies a
playable URL; the current Sentinel contract deliberately does not expose one.
"""

from __future__ import annotations

from homeassistant.components.camera import (
    Camera,
    CameraEntityDescription,
    CameraEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import SentinelError
from .coordinator import SentinelDataUpdateCoordinator
from .entity import SentinelEntity

CAMERA_DESCRIPTION = CameraEntityDescription(key="camera")


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
            SentinelCamera(coordinator, camera_id) for camera_id in new_ids
        )

    _add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_cameras))


class SentinelCamera(SentinelEntity, Camera):
    """Stills, and an optional server-advertised stream, for one camera."""

    _attr_name = None
    """The device is the camera, so the entity takes the device's name rather
    than becoming "Front door Camera"."""

    def __init__(
        self, coordinator: SentinelDataUpdateCoordinator, camera_id: str
    ) -> None:
        super().__init__(coordinator, CAMERA_DESCRIPTION, camera_id=camera_id)
        # Camera.__init__ sets up attributes CoordinatorEntity does not, and
        # neither cooperates through super(); calling it explicitly is what the
        # multiple-inheritance shape used across Home Assistant camera
        # integrations requires.
        Camera.__init__(self)
        camera = self.camera
        self._attr_supported_features = (
            CameraEntityFeature.STREAM
            if camera is not None and camera.stream_url is not None
            else CameraEntityFeature(0)
        )

    @property
    def is_recording(self) -> bool:
        camera = self.camera
        return bool(camera and camera.recording_state == "recording")

    @property
    def motion_detection_enabled(self) -> bool:
        camera = self.camera
        return bool(camera and camera.motion_enabled)

    @property
    def brand(self) -> str | None:
        camera = self.camera
        return camera.manufacturer if camera else None

    @property
    def model(self) -> str | None:
        camera = self.camera
        return camera.model if camera else None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """A still frame, fetched from Sentinel.

        `width`/`height` are ignored deliberately: Sentinel returns the
        substream frame, which is already small, and re-encoding it here to hit
        an exact size would spend CPU on the Home Assistant box to undo the
        saving the substream exists to make. Home Assistant scales what it gets.
        """
        if self._camera_id is None:
            return None
        try:
            return await self.coordinator.client.async_get_snapshot(self._camera_id)
        except SentinelError:
            # Logged by the client with the URL redacted. Returning None shows
            # Home Assistant's own "no image" placeholder, which is the honest
            # answer for a camera that is momentarily unreachable, rather than
            # raising and marking the whole entity unavailable.
            return None

    async def stream_source(self) -> str | None:
        """The RTSP URL Home Assistant should open -- Sentinel's, not the camera's.

        Returning ``None`` disables Home Assistant's stream pipeline while
        leaving snapshots working. This is the normal state with the current
        server contract, which intentionally exposes no cross-host stream URL.
        """
        camera = self.camera
        return camera.stream_url if camera else None
