"""Strict public contract for the experimental local battery provider."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.cameras.tapo_battery import BatteryStatus, RemoteClip
from app.cameras.tapo_timed import TimedRecordingStatus


class BatteryConnectionTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=512)
    username: str | None = Field(default=None, max_length=128)
    child_id: str | None = Field(default=None, max_length=128)
    hub: bool = False


class BatteryChildDevice(BaseModel):
    child_id: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=128)


class BatteryStatusResponse(BaseModel):
    ok: bool = True
    model: str | None
    hardware_version: str | None
    firmware_version: str | None
    battery_percent: int | None
    charging: bool | None
    charging_source: str | None
    read_at: datetime
    transport: str = "local_lan"
    continuous_recording: bool = False
    children: list[BatteryChildDevice] = Field(default_factory=list)

    @classmethod
    def from_status(cls, value: BatteryStatus) -> BatteryStatusResponse:
        return cls(
            model=value.model,
            hardware_version=value.hardware_version,
            firmware_version=value.firmware_version,
            battery_percent=value.battery_percent,
            charging=value.charging,
            charging_source=value.charging_source,
            read_at=value.read_at,
            children=[
                BatteryChildDevice(
                    child_id=child.child_id,
                    name=child.name,
                    model=child.model,
                )
                for child in getattr(value, "children", ())
            ],
        )


class BatteryClipResponse(BaseModel):
    external_id: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    event_type: str
    imported: bool = False
    segment_id: str | None = None

    @classmethod
    def from_clip(
        cls,
        value: RemoteClip,
        *,
        imported: bool = False,
        segment_id: str | None = None,
    ) -> BatteryClipResponse:
        return cls(
            external_id=value.external_id,
            started_at=value.started_at,
            ended_at=value.ended_at,
            duration_seconds=value.duration_seconds,
            event_type=value.event_type,
            imported=imported,
            segment_id=segment_id,
        )


class BatterySyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: int = Field(default=24, ge=1, le=168)
    limit: int = Field(default=20, ge=1, le=100)


class BatterySyncResponse(BaseModel):
    discovered: int
    imported: int
    already_present: int
    failed: int
    segment_ids: list[str]


class TimedRecordingOptions(BaseModel):
    """What the operator may ask for, and what it will cost the battery."""

    presets_minutes: list[int]
    max_minutes: int
    """The lower of the administrator setting and Sentinel's hard ceiling."""
    hard_max_minutes: int
    min_minutes: int
    battery_percent: int | None
    """Charge as of the last successful reading, shown before confirmation."""
    warning: str


class TimedRecordingMaxUpdate(BaseModel):
    """Change the ceiling an administrator may choose from."""

    model_config = ConfigDict(extra="forbid")

    max_minutes: int = Field(ge=1, le=120)


class TimedRecordingStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minutes: int = Field(ge=1, le=120)
    accept_battery_drain: bool = False
    """The administrator's explicit acceptance that this may flatten the battery."""


class TimedRecordingStatusResponse(BaseModel):
    """Live progress, and afterwards an honest account of what was captured."""

    camera_id: str
    state: str
    requested_seconds: int
    started_at: datetime
    ends_at: datetime
    seconds_remaining: int
    seconds_recorded: int
    bytes_recorded: int
    connection: str
    reconnects: int
    battery_percent_at_start: int | None
    message: str
    stop_reason: str | None = None
    segment_id: str | None = None
    finished: bool = False

    @classmethod
    def from_status(cls, value: TimedRecordingStatus) -> TimedRecordingStatusResponse:
        return cls(
            camera_id=value.camera_id,
            state=value.state.value,
            requested_seconds=value.requested_seconds,
            started_at=value.started_at,
            ends_at=value.ends_at,
            seconds_remaining=value.seconds_remaining,
            seconds_recorded=value.seconds_recorded,
            bytes_recorded=value.bytes_recorded,
            connection=value.connection,
            reconnects=value.reconnects,
            battery_percent_at_start=value.battery_percent_at_start,
            message=value.message,
            stop_reason=value.stop_reason.value if value.stop_reason else None,
            segment_id=value.segment_id,
            finished=value.finished,
        )
