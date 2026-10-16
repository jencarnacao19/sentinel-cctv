"""Response and request models for local Tapo camera control.

The whole point of a separate schema module here is that the vendor payload is
never the API. `pytapo` returns nested dictionaries containing the camera's
MAC, its IP, the TP-Link account alias, the device id and the session token;
none of that describes what the camera can *do*, and all of it identifies the
installation. So nothing is passed through: every field below is built
explicitly from a typed value the capability layer produced, and anything the
camera said that is not on this list simply never leaves the process.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.cameras.tapo import (
    DayNightMode,
    TapoCapabilities,
    TapoDetectionState,
    TapoSdCard,
    TapoState,
)


class TapoDeviceResponse(BaseModel):
    """Three fields that describe the hardware, and nothing that identifies it."""

    model: str | None
    hardware_version: str | None
    firmware_version: str | None


class TapoCapabilitiesResponse(BaseModel):
    """What this individual camera positively confirmed it can do.

    A capability absent from ``supported`` is not "unknown, probably fine" --
    it is unsupported. Guessing from a model name is exactly how a control
    appears in the interface and then fails against real hardware.
    """

    supported: list[str]
    device: TapoDeviceResponse
    checked_at: datetime | None
    partial: bool = False
    """True when part of the discovery did not answer.

    The confirmed controls are still real and stay usable; this flag says the
    list may be short, which is a different thing from a camera that answered
    and said no."""
    unchecked: list[str] = Field(default_factory=list)
    """Vendor methods that did not answer during the last discovery."""
    #: Never negotiable, listed so the contract is visible rather than implied.
    excluded: list[str] = Field(
        default_factory=lambda: [
            "firmware_upgrade",
            "firmware_check",
            "sd_card_format",
            "factory_reset",
        ]
    )


class TapoSdCardResponse(BaseModel):
    present: bool
    status: str
    total_gb: float | None
    free_gb: float | None


class TapoDetectionResponse(BaseModel):
    supported: bool
    enabled: bool | None
    sensitivity: int | None
    sensitivity_label: str | None


class TapoStateResponse(BaseModel):
    led_enabled: bool | None
    privacy_mask_enabled: bool | None
    """The camera's own lens mask. A different mechanism on a different device
    from Sentinel's global privacy mode, and deliberately not coupled to it."""
    day_night_mode: str | None
    smart_track_enabled: bool | None
    floodlight_enabled: bool | None
    siren_active: bool | None
    cruise_enabled: bool | None
    preset_count: int | None
    sd_card: TapoSdCardResponse | None
    detections: dict[str, TapoDetectionResponse]
    read_at: datetime | None


class TapoFeatureRequest(BaseModel):
    """One write. Both fields are optional because the families differ.

    Some detections take an on/off flag only, some take a sensitivity as well,
    and the day/night control takes neither -- it takes a mode. The capability
    layer validates the combination per family, because the vendor library
    raises a bare ``Exception`` for a wrong one.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    sensitivity: int | None = Field(default=None, ge=1, le=100)
    sensitivity_label: str | None = Field(default=None, pattern="^(low|normal|high)$")
    mode: DayNightMode | None = None


class TapoSirenRequest(BaseModel):
    """Starting a siren makes noise in someone's home at three in the morning."""

    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(
        description="Must be true. A siren is audible and physical; it is never a default."
    )


class TapoRebootRequest(BaseModel):
    """A reboot drops the camera off every stream for a minute or so."""

    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(description="Must be true. The camera stops recording while it restarts.")


class TapoActionResponse(BaseModel):
    """The honest result of a write.

    ``state`` is the camera's own report *after* the write where one round trip
    could confirm it, and ``null`` where it could not. There is deliberately no
    optimistic echo of the requested value.
    """

    camera_id: str
    feature: str
    applied: bool
    message: str
    state: TapoStateResponse | None = None


def device_response(capabilities: TapoCapabilities) -> TapoDeviceResponse:
    return TapoDeviceResponse(
        model=capabilities.device.model,
        hardware_version=capabilities.device.hardware_version,
        firmware_version=capabilities.device.firmware_version,
    )


def capabilities_response(capabilities: TapoCapabilities) -> TapoCapabilitiesResponse:
    return TapoCapabilitiesResponse(
        supported=sorted(capabilities.supported),
        device=device_response(capabilities),
        checked_at=capabilities.checked_at,
        partial=capabilities.partial,
        unchecked=sorted(capabilities.unavailable),
    )


def _sd_card_response(card: TapoSdCard | None) -> TapoSdCardResponse | None:
    if card is None:
        return None
    return TapoSdCardResponse(
        present=card.present,
        status=card.status,
        total_gb=card.total_gb,
        free_gb=card.free_gb,
    )


def _detection_response(detection: TapoDetectionState) -> TapoDetectionResponse:
    return TapoDetectionResponse(
        supported=detection.supported,
        enabled=detection.enabled,
        sensitivity=detection.sensitivity,
        sensitivity_label=detection.sensitivity_label,
    )


def state_response(state: TapoState) -> TapoStateResponse:
    return TapoStateResponse(
        led_enabled=state.led_enabled,
        privacy_mask_enabled=state.privacy_mask_enabled,
        day_night_mode=state.day_night_mode,
        smart_track_enabled=state.smart_track_enabled,
        floodlight_enabled=state.floodlight_enabled,
        siren_active=state.siren_active,
        cruise_enabled=state.cruise_enabled,
        preset_count=state.preset_count,
        sd_card=_sd_card_response(state.sd_card),
        detections={
            name: _detection_response(detection) for name, detection in state.detections.items()
        },
        read_at=state.read_at,
    )
