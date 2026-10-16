"""Push-first coordinator: entities update on the WebSocket event, not on a timer.

``DataUpdateCoordinator`` is built around polling, so making push the primary
path takes deliberately working against its defaults rather than with them:

* :meth:`_async_update_data` is the REST safety net described in
  :data:`.const.DEFAULT_SCAN_INTERVAL_SECONDS` -- it runs on a timer regardless
  of whether the socket is up, because it is the only thing that notices a
  camera added from Sentinel's own UI or metadata (bitrate, resolution) that no
  event carries.
* Every WebSocket frame instead goes through :meth:`_handle_event`, which
  mutates the already-fetched data in place and calls
  ``async_set_updated_data`` -- the same call a successful poll would make, so
  entities cannot tell the difference between "a poll just landed" and "motion
  started two seconds after the last one".
* While the socket is down the timer shortens to
  :data:`.const.FALLBACK_SCAN_INTERVAL_SECONDS`, and lengthens back once it
  reconnects. That is the literal "falling back to polling" the socket exists
  to make unnecessary the rest of the time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SentinelAuthError,
    SentinelClient,
    SentinelError,
    SentinelEventStream,
)
from .const import (
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_WEBSOCKET_PATH,
    DOMAIN,
    EVENT_MOTION_ENDED,
    EVENT_MOTION_STARTED,
    EVENT_TYPE_CAMERA_OFFLINE,
    EVENT_TYPE_CAMERA_ONLINE,
    EVENT_TYPE_MOTION_ENDED,
    EVENT_TYPE_MOTION_STARTED,
    FALLBACK_SCAN_INTERVAL_SECONDS,
    REFRESH_EVENT_TYPES,
)
from .models import CameraInfo, SentinelEvent, ServerInfo, StorageInfo, has_sequence_gap

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SentinelData:
    """Everything an entity can read from the coordinator."""

    server: ServerInfo
    cameras: dict[str, CameraInfo]
    motion_active: dict[str, bool] = field(default_factory=dict)
    storage: StorageInfo = field(default_factory=StorageInfo)
    retention_days: int | None = None
    event_counts: dict[str, int] = field(default_factory=dict)

    def motion_for(self, camera_id: str) -> bool | None:
        """Local push state wins; the camera's own last-known value is the
        fallback for the moment right after start-up, before any event has
        arrived to populate ``motion_active``."""
        if camera_id in self.motion_active:
            return self.motion_active[camera_id]
        camera = self.cameras.get(camera_id)
        return camera.motion_active if camera is not None else None


class SentinelDataUpdateCoordinator(DataUpdateCoordinator[SentinelData]):
    """One instance per config entry; owns the REST poll and the event socket."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SentinelClient,
        *,
        scan_interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval_seconds),
        )
        self.client = client
        self._configured_interval = timedelta(seconds=scan_interval_seconds)
        self._fallback_interval = timedelta(seconds=FALLBACK_SCAN_INTERVAL_SECONDS)
        self._last_event_seq: int | None = None
        self._stream: SentinelEventStream | None = None
        self._stream_task: asyncio.Task[None] | None = None

    # --- Lifecycle -----------------------------------------------------

    async def async_start_push(self) -> None:
        """Open the event socket. Call once, after the first successful refresh."""
        path = self.data.server.websocket_path or DEFAULT_WEBSOCKET_PATH
        self._stream = SentinelEventStream(
            self.client,
            path=path,
            on_event=self._handle_event,
            on_connect=self._handle_connect,
            on_disconnect=self._handle_disconnect,
        )
        self._stream_task = self.config_entry.async_create_background_task(
            self.hass,
            self._stream.async_run(),
            name=f"{DOMAIN}-events-{self.data.server.unique_id}",
        )

    async def async_stop_push(self) -> None:
        """Close the socket and let its task finish. Call from entry unload."""
        if self._stream is not None:
            await self._stream.async_stop()
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None

    @property
    def push_connected(self) -> bool:
        return self._stream is not None and self._stream.connected

    # --- REST safety net -------------------------------------------------

    async def _async_update_data(self) -> SentinelData:
        try:
            server = await self.client.async_get_server_info()
            cameras = {camera.id: camera for camera in server.cameras}
            if not cameras:
                # Some servers describe cameras only in the dedicated endpoint,
                # keeping the handshake itself small; either shape is honoured.
                cameras = {
                    camera.id: camera
                    for camera in await self.client.async_get_cameras()
                }
            storage_payload, retention_days = await asyncio.gather(
                self.client.async_get_storage(), self.client.async_get_retention_days()
            )
            event_counts = await self._gather_event_counts(cameras)
        except SentinelAuthError as err:
            raise ConfigEntryAuthFailed(
                "Sentinel rejected the API token; re-authenticate this integration"
            ) from err
        except SentinelError as err:
            raise UpdateFailed(str(err)) from err

        previous = self.data
        motion_active = dict(previous.motion_active) if previous is not None else {}
        # A camera Sentinel no longer reports cannot still be "in motion".
        for camera_id in list(motion_active):
            if camera_id not in cameras:
                motion_active.pop(camera_id, None)

        return SentinelData(
            server=server,
            cameras=cameras,
            motion_active=motion_active,
            storage=StorageInfo.from_payload(storage_payload),
            retention_days=retention_days,
            event_counts=event_counts,
        )

    async def _gather_event_counts(
        self, cameras: dict[str, CameraInfo]
    ) -> dict[str, int]:
        if not cameras:
            return {}
        results = await asyncio.gather(
            *(self.client.async_get_event_count(camera_id) for camera_id in cameras),
            return_exceptions=True,
        )
        counts: dict[str, int] = {}
        for camera_id, result in zip(cameras, results, strict=True):
            if isinstance(result, int):
                counts[camera_id] = result
            elif isinstance(result, SentinelError):
                # One camera's count endpoint misbehaving should not cost the
                # whole refresh; its sensor just stays at its last known value.
                _LOGGER.debug(
                    "Could not read the event count for camera %s: %s",
                    camera_id,
                    result,
                )
            elif isinstance(result, BaseException):
                raise result
        return counts

    # --- Push events -------------------------------------------------------

    def _handle_connect(self) -> None:
        _LOGGER.debug("Sentinel event socket connected")
        self.update_interval = self._configured_interval
        # Catch up on anything that changed while the socket was re-establishing;
        # harmless if nothing did, since the coordinator collapses overlapping
        # refresh requests into one.
        self.hass.async_create_task(self.async_request_refresh())

    def _handle_disconnect(self) -> None:
        _LOGGER.debug("Sentinel event socket disconnected; falling back to polling")
        self.update_interval = self._fallback_interval

    def _handle_event(self, event: SentinelEvent) -> None:
        if has_sequence_gap(self._last_event_seq, event.seq):
            _LOGGER.debug(
                "Sentinel event sequence jumped (%s -> %s); resynchronising over REST",
                self._last_event_seq,
                event.seq,
            )
            self.hass.async_create_task(self.async_request_refresh())
        if event.seq is not None:
            self._last_event_seq = event.seq

        camera_id = event.camera_id
        if event.type == EVENT_TYPE_MOTION_STARTED and camera_id:
            self._set_motion(camera_id, True, event)
        elif event.type == EVENT_TYPE_MOTION_ENDED and camera_id:
            self._set_motion(camera_id, False, event)
        elif event.type == EVENT_TYPE_CAMERA_ONLINE and camera_id:
            self._set_camera_status(camera_id, online=True)
        elif event.type == EVENT_TYPE_CAMERA_OFFLINE and camera_id:
            self._set_camera_status(camera_id, online=False)
        elif event.type in REFRESH_EVENT_TYPES:
            self.hass.async_create_task(self.async_request_refresh())
        # Any other type -- including one this build has never heard of -- is
        # ignored outright, which is what lets a newer server add event types
        # without breaking an older integration.

    def _set_motion(self, camera_id: str, active: bool, event: SentinelEvent) -> None:
        if camera_id not in self.data.cameras:
            # A frame about a camera that vanished between events and the next
            # poll; the coming refresh will clean up the dict either way.
            return
        self.data.motion_active[camera_id] = active
        self.async_set_updated_data(self.data)
        self.hass.bus.async_fire(
            EVENT_MOTION_STARTED if active else EVENT_MOTION_ENDED,
            {
                "camera_id": camera_id,
                "camera_name": event.camera_name or self.data.cameras[camera_id].name,
                "event_id": event.event_id,
                "timestamp": event.timestamp,
            },
        )

    def _set_camera_status(self, camera_id: str, *, online: bool) -> None:
        camera = self.data.cameras.get(camera_id)
        if camera is None:
            return
        status = "online" if online else "offline"
        if camera.status == status:
            return
        self.data.cameras[camera_id] = replace(camera, status=status)
        self.async_set_updated_data(self.data)
