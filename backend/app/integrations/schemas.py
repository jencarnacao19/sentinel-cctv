"""Response shapes for the Home Assistant handshake.

Pinned to ``CONTRACT.md`` -- if either changes, check both together.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.database.models import CameraStatus, RecordingMode

#: Bumped only when a field changes shape or meaning in a way older client
#: code would misread; see CONTRACT.md section 6.
API_VERSION = 1

#: The only values `recording_state` ever takes. An HA sensor with a declared
#: option list rejects anything outside it, so a value this server has never
#: heard of is worse than one it simply omits.
RECORDING_STATES: tuple[str, ...] = ("recording", "idle", "off", "unavailable")


class HaCameraStream(BaseModel):
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_codec: str | None = None
    bitrate_kbps: int | None = None


class HaCameraCapabilities(BaseModel):
    snapshot: bool
    live_stream: bool
    recording: bool
    motion_events: bool


class HaCamera(BaseModel):
    id: str
    name: str
    enabled: bool
    status: CameraStatus
    manufacturer: str | None = None
    model: str | None = None
    recording_mode: RecordingMode
    recording_state: str
    motion_enabled: bool
    motion_active: bool
    capabilities: HaCameraCapabilities
    stream: HaCameraStream


class HaHandshake(BaseModel):
    unique_id: str
    name: str = "Sentinel CCTV"
    version: str
    api_version: int = API_VERSION
    websocket_path: str
    cameras: list[HaCamera] = Field(default_factory=list)
