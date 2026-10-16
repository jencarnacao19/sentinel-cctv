"""Request and response models for discovery.

Response models list fields explicitly rather than serialising internal
dataclasses, for the same reason ``app.cameras.schemas`` does: a password must
never gain a path into a response body just because it happened to be an
attribute of something that got handed to a serialiser.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.cameras.probe import ProbeResult
from app.cameras.schemas import CameraCreate, CameraResponse
from app.discovery import profiles
from app.discovery.onvif import MediaProfile
from app.discovery.scanner import CandidateResult, ChannelResult, NvrScanResult
from app.discovery.wsdiscovery import DiscoveryResult, ProbeMatch

Credential = Annotated[str, Field(max_length=512)]
Port = Annotated[int, Field(ge=1, le=65535)]


def _validate_host(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("Host is required.")
    if len(stripped) > 255:
        raise ValueError("Host is too long.")
    if any(char in stripped for char in ("\n", "\r", "\x00", " ", "/")):
        raise ValueError("Host must be a bare address or hostname, not a URL.")
    return stripped


# --- Vendor metadata --------------------------------------------------------


class VendorsResponse(BaseModel):
    """The known-pattern and NVR vendor lists, so the interface never hardcodes
    its own copy that could drift from :mod:`app.discovery.profiles`."""

    known_stream_vendors: list[str]
    nvr_vendors: list[str]


# --- WS-Discovery scan ----------------------------------------------------


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=4.0, ge=1.0, le=15.0)


class DiscoveredDeviceResponse(BaseModel):
    uuid: str | None
    endpoint: str
    xaddrs: list[str]
    scopes: list[str]
    manufacturer: str | None
    model: str | None
    name: str | None

    @classmethod
    def from_match(cls, match: ProbeMatch) -> DiscoveredDeviceResponse:
        return cls(
            uuid=match.uuid,
            endpoint=match.endpoint,
            xaddrs=list(match.xaddrs),
            scopes=list(match.scopes),
            manufacturer=match.manufacturer,
            model=match.hardware,
            name=match.name,
        )


class ScanResponse(BaseModel):
    devices: list[DiscoveredDeviceResponse]
    interfaces_probed: list[str]

    @classmethod
    def from_result(cls, result: DiscoveryResult) -> ScanResponse:
        return cls(
            devices=[DiscoveredDeviceResponse.from_match(match) for match in result.devices],
            interfaces_probed=result.interfaces_probed,
        )


# --- ONVIF media profiles ---------------------------------------------------


class OnvifProfilesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    xaddr: str = Field(max_length=2048)
    username: str | None = Field(default=None, max_length=128)
    password: Credential | None = None
    timeout_seconds: float = Field(default=10.0, ge=2.0, le=30.0)

    @model_validator(mode="after")
    def _check_xaddr(self) -> Self:
        from urllib.parse import urlsplit

        parts = urlsplit(self.xaddr.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("xaddr must be an http(s) URL, as returned by WS-Discovery.")
        return self


class MediaProfileResponse(BaseModel):
    token: str
    name: str | None
    encoding: str | None
    width: int | None
    height: int | None
    fps: float | None
    stream_uri: str

    @classmethod
    def from_profile(cls, profile: MediaProfile) -> MediaProfileResponse:
        return cls(
            token=profile.token,
            name=profile.name,
            encoding=profile.encoding,
            width=profile.width,
            height=profile.height,
            fps=profile.fps,
            stream_uri=profile.stream_uri,
        )


class OnvifProfilesResponse(BaseModel):
    profiles: list[MediaProfileResponse]


# --- Known vendor pattern probing -------------------------------------------


class KnownProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(max_length=255)
    port: Port = 554
    username: str | None = Field(default=None, max_length=128)
    password: Credential | None = None
    vendor: str | None = None
    timeout_seconds: float = Field(default=6.0, ge=1.0, le=20.0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        _validate_host(self.host)
        if self.vendor is not None and self.vendor not in profiles.KNOWN_VENDORS:
            known = ", ".join(profiles.KNOWN_VENDORS)
            raise ValueError(f"Unknown vendor '{self.vendor}'. Known: {known}.")
        return self


class ProbeStreamInfo(BaseModel):
    codec: str | None = None
    resolution: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_codec: str | None = None
    bitrate_kbps: int | None = None
    can_stream_copy: bool = False

    @classmethod
    def from_probe_result(cls, result: ProbeResult) -> ProbeStreamInfo:
        return cls(
            codec=result.codec,
            resolution=result.resolution,
            width=result.width,
            height=result.height,
            fps=result.fps,
            audio_codec=result.audio_codec,
            bitrate_kbps=result.bitrate_kbps,
            can_stream_copy=result.can_stream_copy,
        )


class KnownProbeCandidateResponse(BaseModel):
    vendor: str
    label: str
    path: str
    url: str
    stream: ProbeStreamInfo

    @classmethod
    def from_candidate(cls, candidate: CandidateResult) -> KnownProbeCandidateResponse:
        return cls(
            vendor=candidate.vendor,
            label=candidate.label,
            path=candidate.path,
            url=candidate.url,
            stream=ProbeStreamInfo.from_probe_result(candidate.result),
        )


class KnownProbeResponse(BaseModel):
    candidates: list[KnownProbeCandidateResponse]


# --- NVR multi-channel scan --------------------------------------------------


class NvrScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(max_length=255)
    port: Port = 554
    username: str | None = Field(default=None, max_length=128)
    password: Credential | None = None
    vendor: str
    sub: bool = False
    start_channel: int = Field(default=1, ge=1, le=999)
    max_channels: int = Field(default=16, ge=1, le=64)
    max_consecutive_failures: int = Field(default=2, ge=1, le=10)
    timeout_seconds: float = Field(default=6.0, ge=1.0, le=20.0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        _validate_host(self.host)
        if self.vendor not in profiles.NVR_VENDORS:
            known = ", ".join(profiles.NVR_VENDORS)
            raise ValueError(f"Unknown NVR vendor '{self.vendor}'. Known: {known}.")
        return self


class NvrChannelResponse(BaseModel):
    channel: int
    path: str
    url: str
    stream: ProbeStreamInfo

    @classmethod
    def from_channel(cls, channel: ChannelResult) -> NvrChannelResponse:
        return cls(
            channel=channel.channel,
            path=channel.path,
            url=channel.url,
            stream=ProbeStreamInfo.from_probe_result(channel.result),
        )


class NvrScanResponse(BaseModel):
    channels: list[NvrChannelResponse]
    checked: int
    stopped_reason: str

    @classmethod
    def from_result(cls, result: NvrScanResult) -> NvrScanResponse:
        return cls(
            channels=[NvrChannelResponse.from_channel(channel) for channel in result.channels],
            checked=result.checked,
            stopped_reason=result.stopped_reason,
        )


# --- Import ------------------------------------------------------------------


class ImportCamerasRequest(BaseModel):
    """A batch of cameras to create from whatever the user chose to keep out
    of a scan. Reuses :class:`CameraCreate` wholesale rather than redefining
    the same validation (scheme allow-list, length caps, credential
    handling) a second time."""

    model_config = ConfigDict(extra="forbid")

    cameras: list[CameraCreate] = Field(min_length=1, max_length=64)


class ImportFailure(BaseModel):
    name: str
    error: str


class ImportCamerasResponse(BaseModel):
    created: list[CameraResponse]
    failed: list[ImportFailure]
