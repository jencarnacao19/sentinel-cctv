"""Typed views over the Sentinel REST and WebSocket payloads.

Parsing lives here, apart from the transport, for two reasons: it is the part
most likely to meet a payload from a Sentinel version this integration has never
seen, and it is the part worth testing without a Home Assistant installation.

Every field except the identity of a camera is optional. A server that adds,
renames or drops a field must degrade one entity to *unknown*, never break the
whole integration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .const import (
    DEFAULT_WEBSOCKET_PATH,
    EVENT_TYPE_MOTION_ENDED,
    EVENT_TYPE_MOTION_STARTED,
    STATUS_ONLINE,
)

#: Values the recording sensor may report. Anything else the server invents is
#: mapped to ``None`` (unknown) rather than surfaced, because Home Assistant
#: rejects an enum state that is not in the declared option list.
RECORDING_STATES: tuple[str, ...] = ("recording", "idle", "off", "unavailable")


def _as_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _as_bool(value: Any, default: bool | None = None) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    return default


def _as_int(value: Any) -> int | None:
    # bool is an int subclass; a boolean here means the field was misunderstood.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """Characteristics of the main stream, from Sentinel's last probe."""

    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_codec: str | None = None
    bitrate_kbps: int | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> StreamInfo:
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            codec=_as_str(payload.get("codec")),
            width=_as_int(payload.get("width")),
            height=_as_int(payload.get("height")),
            fps=_as_float(payload.get("fps")),
            audio_codec=_as_str(payload.get("audio_codec")),
            bitrate_kbps=_as_int(payload.get("bitrate_kbps")),
        )

    @property
    def resolution(self) -> str | None:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None


@dataclass(frozen=True, slots=True)
class MotionZoneInfo:
    """A named detection zone on a camera. Each gets its own motion sensor."""

    id: str
    name: str
    active: bool = False


def _parse_zones(value: Any) -> tuple[MotionZoneInfo, ...]:
    """Zones from a handshake entry; anything unusable is skipped."""
    if not isinstance(value, list):
        return ()
    zones: list[MotionZoneInfo] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        zone_id = _as_str(entry.get("id"))
        if zone_id is None:
            continue
        zones.append(
            MotionZoneInfo(
                id=zone_id,
                name=_as_str(entry.get("name")) or zone_id,
                active=bool(_as_bool(entry.get("active"), False)),
            )
        )
    return tuple(zones)


@dataclass(frozen=True, slots=True)
class CameraInfo:
    """One camera as Sentinel describes it."""

    id: str
    name: str
    enabled: bool = True
    status: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    recording_mode: str | None = None
    recording_state: str | None = None
    motion_enabled: bool | None = None
    motion_active: bool | None = None
    motion_zones: tuple[MotionZoneInfo, ...] = ()
    stream: StreamInfo = field(default_factory=StreamInfo)
    stream_url: str | None = None
    """Playable stream for the HA stream component, when the server offers one."""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CameraInfo:
        """Build from a ``/cameras`` entry.

        Raises:
            ValueError: the entry has no usable id, which makes it impossible to
                key an entity on and impossible to talk to.
        """
        camera_id = _as_str(payload.get("id"))
        if camera_id is None:
            raise ValueError("Camera payload has no id")
        return cls(
            id=camera_id,
            # A camera with no name still needs a stable, human-readable label.
            name=_as_str(payload.get("name")) or f"Camera {camera_id[:8]}",
            enabled=bool(_as_bool(payload.get("enabled"), True)),
            status=_as_str(payload.get("status")),
            manufacturer=_as_str(payload.get("manufacturer")),
            model=_as_str(payload.get("model")),
            recording_mode=_as_str(payload.get("recording_mode")),
            recording_state=_as_str(payload.get("recording_state")),
            motion_enabled=_as_bool(payload.get("motion_enabled")),
            motion_active=_as_bool(payload.get("motion_active")),
            motion_zones=_parse_zones(payload.get("motion_zones")),
            stream=StreamInfo.from_payload(payload.get("stream")),
            stream_url=_as_str(payload.get("stream_url"))
            or _as_str(payload.get("hls_url")),
        )

    @property
    def is_online(self) -> bool:
        return self.status == STATUS_ONLINE

    def zone(self, zone_id: str) -> MotionZoneInfo | None:
        return next((zone for zone in self.motion_zones if zone.id == zone_id), None)


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """The handshake payload from ``GET /api/v1/integrations/home-assistant``."""

    unique_id: str
    version: str | None = None
    name: str | None = None
    websocket_path: str = DEFAULT_WEBSOCKET_PATH
    cameras: tuple[CameraInfo, ...] = ()
    api_version: int | None = None
    """The Home Assistant integration protocol version the server speaks.

    ``None`` means the server predates this field; treated as compatible rather
    than rejected, since every payload field here is optional by design and an
    absent version is not the same claim as an incompatible one."""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ServerInfo:
        """Build from the integration handshake.

        Raises:
            ValueError: no ``unique_id``. Without it two Sentinel servers on the
                same network cannot be told apart, and a config entry could bind
                to the wrong one after a DHCP lease changes.
        """
        unique_id = _as_str(payload.get("unique_id"))
        if unique_id is None:
            raise ValueError("Handshake payload has no unique_id")
        websocket_path = (
            _as_str(payload.get("websocket_path")) or DEFAULT_WEBSOCKET_PATH
        )
        raw_cameras = payload.get("cameras")
        return cls(
            unique_id=unique_id,
            version=_as_str(payload.get("version")),
            name=_as_str(payload.get("name")),
            websocket_path=websocket_path,
            cameras=parse_cameras(raw_cameras),
            api_version=_as_int(payload.get("api_version")),
        )


def parse_cameras(payload: Any) -> tuple[CameraInfo, ...]:
    """Parse a camera list, dropping entries that cannot be identified.

    One malformed camera must not hide the other twenty.
    """
    if isinstance(payload, Mapping):
        payload = payload.get("items", payload.get("cameras"))
    if not isinstance(payload, list):
        return ()
    cameras: list[CameraInfo] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        try:
            cameras.append(CameraInfo.from_payload(entry))
        except ValueError:
            continue
    return tuple(cameras)


@dataclass(frozen=True, slots=True)
class SentinelEvent:
    """One WebSocket frame."""

    type: str
    seq: int | None = None
    ts: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> SentinelEvent:
        """Build from a decoded frame.

        Raises:
            ValueError: the frame has no ``type``, so there is nothing to route.
        """
        if not isinstance(payload, Mapping):
            # ValueError, not TypeError, despite this being a type check: the
            # contract of this method is "raises ValueError for any unusable
            # frame", and the caller catches exactly that. Splitting the
            # failures by *kind* of malformation would make every caller catch
            # two exceptions for one idea.
            raise ValueError("WebSocket frame is not an object")  # noqa: TRY004
        event_type = _as_str(payload.get("type"))
        if event_type is None:
            raise ValueError("WebSocket frame has no type")
        data = payload.get("data")
        return cls(
            type=event_type,
            seq=_as_int(payload.get("seq")),
            ts=_as_str(payload.get("ts")),
            data=data if isinstance(data, Mapping) else {},
        )

    @property
    def camera_id(self) -> str | None:
        return _as_str(self.data.get("camera_id")) or _as_str(self.data.get("id"))

    @property
    def camera_name(self) -> str | None:
        return _as_str(self.data.get("camera_name")) or _as_str(self.data.get("name"))

    @property
    def event_id(self) -> str | None:
        return _as_str(self.data.get("event_id")) or _as_str(
            self.data.get("motion_event_id")
        )

    @property
    def zone_id(self) -> str | None:
        return _as_str(self.data.get("zone_id"))

    @property
    def zone_name(self) -> str | None:
        return _as_str(self.data.get("zone"))

    @property
    def timestamp(self) -> str | None:
        return _as_str(self.data.get("timestamp")) or self.ts

    @property
    def is_motion(self) -> bool:
        return self.type in (EVENT_TYPE_MOTION_STARTED, EVENT_TYPE_MOTION_ENDED)


def has_sequence_gap(previous: int | None, current: int | None) -> bool:
    """Whether frames were missed between ``previous`` and ``current``.

    A sequence that jumps forward means dropped frames; one that goes backwards
    means the server restarted its counter. Both leave the local state possibly
    wrong, and both are answered the same way: re-read everything over REST.
    """
    if previous is None or current is None:
        return False
    return current != previous + 1


@dataclass(frozen=True, slots=True)
class StorageInfo:
    """The ``GET /storage`` payload: how much room Sentinel's footage is using.

    Every figure is optional -- a storage volume that has gone missing reports
    ``available=False`` and nothing else may be known, and the sensors have to
    degrade to *unknown* rather than show a stale byte count as current.
    """

    available: bool = True
    reason: str | None = None
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    recordings_bytes: int | None = None
    segment_count: int | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> StorageInfo:
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            available=bool(_as_bool(payload.get("available"), True)),
            reason=_as_str(payload.get("reason")),
            total_bytes=_as_int(payload.get("total_bytes")),
            used_bytes=_as_int(payload.get("used_bytes")),
            free_bytes=_as_int(payload.get("free_bytes")),
            recordings_bytes=_as_int(payload.get("recordings_bytes")),
            segment_count=_as_int(payload.get("segment_count")),
        )


def derive_recording_state(
    camera: CameraInfo, *, motion_active: bool | None = None
) -> str | None:
    """Best available answer to "is this camera being recorded right now?".

    The server's own ``recording_state`` wins when it sends one. Otherwise it is
    inferred from the recording mode, because that is all an older server offers.
    Unrecognised server values become ``None`` (unknown) rather than being passed
    through, since Home Assistant refuses an enum state outside the option list.
    """
    if camera.recording_state is not None:
        state = camera.recording_state.lower()
        return state if state in RECORDING_STATES else None

    mode = (camera.recording_mode or "").lower()
    if not camera.enabled or mode == "off":
        return "off"
    if camera.status is not None and not camera.is_online:
        return "unavailable"
    if mode == "continuous":
        return "recording"
    if mode == "motion":
        if motion_active is None:
            return None
        return "recording" if motion_active else "idle"
    if mode == "scheduled":
        return "idle"
    return None
