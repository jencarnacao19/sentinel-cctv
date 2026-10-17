"""Request and response models for cameras.

The response model is the security boundary: it lists fields explicitly rather
than serialising the ORM object, so a password can never be added to the table
and leak through the API by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.cameras.rtsp import (
    ALLOWED_STREAM_SCHEMES,
    InvalidStreamUrlError,
    parse_stream_url,
)
from app.cameras.vendor import TapoControlSource, classify_tapo_controls
from app.database.models import (
    Camera,
    CameraKind,
    CameraStatus,
    RecordingMode,
    RecordingProfile,
    RecordingSourceMode,
)

Sensitivity = Annotated[int, Field(ge=0, le=100)]
TapoPatrolMode = Literal["horizontal", "vertical"]

#: What a recorder is doing right now. ``failed`` means the camera is configured
#: to record and is not recording -- an honest state the interface must be able
#: to show, rather than leaving a switch on and pretending. Defined here rather
#: than in the recording package because the response model needs it at import
#: time and that package imports this one.
RecordingRuntimeState = Literal["recording", "off", "failed", "paused", "unsupported"]


class TapoControlAvailability(BaseModel):
    """Whether Sentinel offers Tapo LAN controls for a camera, and why.

    The reason travels with the answer so the interface never has to reconstruct
    it, and so "we could not tell" is visibly different from "this is a
    Hikvision channel".
    """

    supported: bool
    source: TapoControlSource
    detected_vendor: str | None
    reason: str
    can_opt_in: bool
    """Whether offering the explicit opt-in would be honest for this camera."""

    @classmethod
    def from_camera(cls, camera: Camera) -> TapoControlAvailability:
        classification = classify_tapo_controls(camera)
        return cls(
            supported=classification.supported,
            source=classification.source,
            detected_vendor=classification.detected_vendor,
            reason=classification.reason,
            can_opt_in=classification.can_opt_in,
        )


def _recording_state(camera: Camera) -> RecordingRuntimeState:
    """Ask the recording manager, which is the only honest source."""
    # Imported here: app.recording imports app.cameras.service, so a module-
    # level import would close a cycle.
    from app.recording.manager import get_manager

    return get_manager().camera_recording_state(camera)


def _validate_host(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().rstrip(".")
    if not stripped:
        return None
    parts = urlsplit(f"//{stripped}")
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.port is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise ValueError("host must be a bare local IP address or hostname without a port.")
    return stripped


def _validate_url(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parse_stream_url(stripped, allowed_schemes=ALLOWED_STREAM_SCHEMES)
    except InvalidStreamUrlError as exc:
        raise ValueError(f"{field_name}: {exc}") from exc
    return stripped


def _validate_onvif_xaddr(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    parts = urlsplit(stripped)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("onvif_device_xaddr must be an http(s) ONVIF service URL.")
    if parts.username or parts.password:
        raise ValueError("onvif_device_xaddr must not contain credentials.")
    return stripped


class CameraBase(BaseModel):
    name: str = Field(min_length=1, max_length=96)
    manufacturer: str | None = Field(default=None, max_length=96)
    model: str | None = Field(default=None, max_length=96)
    nvr_group: str | None = Field(default=None, max_length=96)
    enabled: bool = True
    recording_mode: RecordingMode = RecordingMode.CONTINUOUS
    recording_profile: RecordingProfile = RecordingProfile.AUTO
    recording_source_mode: RecordingSourceMode = RecordingSourceMode.AUTO
    recording_segment_seconds: int | None = Field(default=None, ge=10, le=300)
    recording_enabled: bool = True
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    motion_enabled: bool = True
    motion_sensitivity: Sensitivity = 50
    tapo_patrol_mode: TapoPatrolMode | None = None
    tapo_controls_enabled: bool | None = None
    """Explicit operator answer. ``None`` leaves the decision to detection."""


class CameraCreate(CameraBase):
    """Adding a camera.

    Credentials may arrive either as separate fields or embedded in the URL --
    people paste whatever their camera's app gave them. Either way they are
    lifted out and encrypted, and the stored URL is left clean.
    """

    kind: CameraKind = CameraKind.RTSP
    host: str | None = Field(default=None, max_length=255)
    main_stream_url: str | None = Field(default=None, max_length=2048)
    sub_stream_url: str | None = Field(default=None, max_length=2048)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    tapo_password: str | None = Field(default=None, max_length=512)
    tapo_child_id: str | None = Field(default=None, max_length=128)
    onvif_device_xaddr: str | None = Field(default=None, max_length=2048)
    onvif_profile_token: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _check_urls(self) -> Self:
        self.host = _validate_host(self.host)
        if self.kind is CameraKind.RTSP:
            if not self.main_stream_url:
                raise ValueError("main_stream_url is required for an RTSP camera.")
            _validate_url(self.main_stream_url, "main_stream_url")
            _validate_url(self.sub_stream_url, "sub_stream_url")
            if self.host is not None or self.tapo_child_id is not None:
                raise ValueError("host and tapo_child_id are only for Tapo battery cameras.")
        else:
            if not self.host:
                raise ValueError("host is required for a Tapo battery camera or its local hub.")
            if not self.tapo_password:
                raise ValueError("tapo_password is required for a Tapo battery camera.")
            # Tapo battery cameras default to "admin" for local Third-Party Compatibility,
            # or can accept a Tapo cloud account email / username when Third-Party
            # Compatibility is off. Normalize empty string to None.
            if self.username is not None:
                cleaned = self.username.strip()
                self.username = cleaned if cleaned else None
            if (
                self.main_stream_url
                or self.sub_stream_url
                or self.onvif_device_xaddr
                or self.onvif_profile_token
            ):
                raise ValueError("A Tapo battery camera must not be given an RTSP or ONVIF URL.")
            if self.password:
                raise ValueError("Use tapo_password for a Tapo battery camera.")
            if self.tapo_patrol_mode:
                raise ValueError("Tapo battery cameras do not support continuous patrol.")
            self.recording_mode = RecordingMode.EVENTS
            self.motion_enabled = False
        self.onvif_device_xaddr = _validate_onvif_xaddr(self.onvif_device_xaddr)
        return self


class CameraUpdate(BaseModel):
    """Partial update. An omitted field is left alone.

    ``password`` and ``tapo_password`` distinguish three cases: omitted means
    keep the stored value, a string sets a new one, and an explicit empty string
    clears it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=96)
    manufacturer: str | None = Field(default=None, max_length=96)
    model: str | None = Field(default=None, max_length=96)
    nvr_group: str | None = Field(default=None, max_length=96)
    host: str | None = Field(default=None, max_length=255)
    main_stream_url: str | None = Field(default=None, max_length=2048)
    sub_stream_url: str | None = Field(default=None, max_length=2048)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    tapo_password: str | None = Field(default=None, max_length=512)
    tapo_child_id: str | None = Field(default=None, max_length=128)
    onvif_device_xaddr: str | None = Field(default=None, max_length=2048)
    onvif_profile_token: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None
    recording_mode: RecordingMode | None = None
    recording_profile: RecordingProfile | None = None
    recording_source_mode: RecordingSourceMode | None = None
    recording_segment_seconds: int | None = Field(default=None, ge=10, le=300)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    motion_enabled: bool | None = None
    motion_sensitivity: Sensitivity | None = None
    tapo_patrol_mode: TapoPatrolMode | None = None
    tapo_controls_enabled: bool | None = None

    @model_validator(mode="after")
    def _check_urls(self) -> Self:
        if "recording_source_mode" in self.model_fields_set and self.recording_source_mode is None:
            raise ValueError(
                "recording_source_mode must be auto, relay, or direct; "
                "omit it to keep the current value."
            )
        if self.main_stream_url is not None:
            _validate_url(self.main_stream_url, "main_stream_url")
        if self.host is not None:
            self.host = _validate_host(self.host)
        if self.sub_stream_url:
            _validate_url(self.sub_stream_url, "sub_stream_url")
        if self.onvif_device_xaddr is not None:
            self.onvif_device_xaddr = _validate_onvif_xaddr(self.onvif_device_xaddr)
        return self


class CameraStreamInfo(BaseModel):
    """Last known stream characteristics, from the most recent successful probe."""

    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_codec: str | None = None
    bitrate_kbps: int | None = None


class CameraResponse(CameraBase):
    """A camera as the API returns it. Deliberately never carries a password."""

    id: str
    kind: CameraKind
    host: str
    main_stream_url: str | None
    sub_stream_url: str | None
    username: str | None
    has_credentials: bool
    """True when a password is stored. The password itself is never returned."""
    has_tapo_credentials: bool
    tapo_child_id: str | None
    """True when a separate Tapo control password is stored; never returns it."""
    status: CameraStatus
    status_message: str | None
    last_seen_at: datetime | None
    stream: CameraStreamInfo
    created_at: datetime
    updated_at: datetime
    onvif_device_xaddr: str | None
    ptz_supported: bool | None
    ptz_zoom_supported: bool
    ptz_status_message: str | None
    ptz_last_checked_at: datetime | None
    onvif_patrol_enabled: bool
    tapo_controls: TapoControlAvailability
    """Whether Sentinel offers this camera's vendor controls, and why."""
    recording_state: RecordingRuntimeState
    """What the recorder is actually doing, not merely what was configured."""

    @classmethod
    def from_camera(cls, camera: Camera) -> CameraResponse:
        return cls(
            id=camera.id,
            kind=camera.kind,
            name=camera.name,
            manufacturer=camera.manufacturer,
            model=camera.model,
            nvr_group=camera.nvr_group,
            host=camera.host,
            main_stream_url=camera.main_stream_url,
            sub_stream_url=camera.sub_stream_url,
            username=camera.username,
            has_credentials=bool(camera.encrypted_password),
            has_tapo_credentials=bool(camera.encrypted_tapo_password),
            tapo_child_id=camera.tapo_child_id,
            onvif_device_xaddr=camera.onvif_device_xaddr,
            ptz_supported=camera.ptz_supported,
            ptz_zoom_supported=camera.ptz_zoom_supported,
            ptz_status_message=camera.ptz_status_message,
            ptz_last_checked_at=camera.ptz_last_checked_at,
            onvif_patrol_enabled=bool(camera.onvif_patrol_tour_token),
            tapo_controls=TapoControlAvailability.from_camera(camera),
            recording_state=_recording_state(camera),
            enabled=camera.enabled,
            recording_mode=camera.recording_mode,
            recording_profile=camera.recording_profile,
            recording_source_mode=camera.recording_source_mode,
            recording_segment_seconds=camera.recording_segment_seconds,
            recording_enabled=camera.recording_enabled,
            retention_days=camera.retention_days,
            motion_enabled=camera.motion_enabled,
            motion_sensitivity=camera.motion_sensitivity,
            tapo_patrol_mode=camera.tapo_patrol_mode,
            tapo_controls_enabled=camera.tapo_controls_enabled,
            status=camera.status,
            status_message=camera.status_message,
            last_seen_at=camera.last_seen_at,
            stream=CameraStreamInfo(
                codec=camera.codec,
                width=camera.width,
                height=camera.height,
                fps=camera.fps,
                audio_codec=camera.audio_codec,
                bitrate_kbps=camera.bitrate_kbps,
            ),
            created_at=camera.created_at,
            updated_at=camera.updated_at,
        )


class CameraRecordingRequest(BaseModel):
    """Turn one camera's recorder on or off."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    confirm: bool = False
    """Required to stop recording. Losing footage should take a decision."""


class CameraRecordingResponse(BaseModel):
    """What the recorder is doing after the change was applied."""

    camera_id: str
    recording_enabled: bool
    """The persisted setting."""
    state: RecordingRuntimeState
    """The observed process state, which may disagree with the setting."""
    message: str


class CameraIdentityResponse(BaseModel):
    """What the camera said about itself over ONVIF."""

    camera_id: str
    manufacturer: str | None
    model: str | None
    firmware_version: str | None
    tapo_controls: TapoControlAvailability
    message: str


class PtzCapabilityResponse(BaseModel):
    supported: bool
    zoom_supported: bool
    preset_capacity: int = 0
    preset_tour_capacity: int = 0
    home_supported: bool = False
    message: str
    checked_at: datetime


class PtzPresetResponse(BaseModel):
    token: str
    name: str


class PtzSetupResponse(BaseModel):
    presets: list[PtzPresetResponse]
    preset_capacity: int
    preset_tour_capacity: int
    home_supported: bool
    patrol_backend: Literal["onvif_lan", "tapo_lan", "none"]


class PtzPresetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)


class PtzPatrolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_tokens: list[str] = Field(min_length=2, max_length=32)
    stay_seconds: int = Field(default=10, ge=2, le=300)
    name: str = Field(default="Sentinel local patrol", min_length=1, max_length=64)

    @model_validator(mode="after")
    def _unique_presets(self) -> Self:
        if len(set(self.preset_tokens)) != len(self.preset_tokens):
            raise ValueError("Patrol presets must be unique.")
        return self


class PtzPatrolResponse(BaseModel):
    enabled: bool
    backend: Literal["onvif_lan", "tapo_lan", "none"]


class PtzMoveRequest(BaseModel):
    """A normalised ONVIF velocity vector."""

    model_config = ConfigDict(extra="forbid")

    pan: float = Field(default=0.0, ge=-1.0, le=1.0)
    tilt: float = Field(default=0.0, ge=-1.0, le=1.0)
    zoom: float = Field(default=0.0, ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def _has_motion(self) -> Self:
        if abs(self.pan) < 0.001 and abs(self.tilt) < 0.001 and abs(self.zoom) < 0.001:
            raise ValueError("At least one PTZ velocity must be non-zero.")
        return self


class StreamTestRequest(BaseModel):
    """Test an arbitrary URL before committing to saving it."""

    url: str = Field(max_length=2048)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _check_url(self) -> Self:
        _validate_url(self.url, "url")
        return self


class StreamTestResponse(BaseModel):
    """The result shown in the "test connection" panel."""

    ok: bool
    error: str | None = None
    codec: str | None = None
    resolution: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_codec: str | None = None
    bitrate_kbps: int | None = None
    can_stream_copy: bool = False
    """False means recording this camera would need re-encoding."""


class CameraTestResponse(BaseModel):
    """Both streams of a saved camera, tested together."""

    main: StreamTestResponse
    sub: StreamTestResponse | None = None
