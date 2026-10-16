"""Response models for the read-only viewer security boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from app.cameras.schemas import CameraStreamInfo
from app.database.models import Camera, CameraKind, CameraStatus


class ViewerCameraResponse(BaseModel):
    """Only what is needed to render a live tile.

    Stream URLs, camera hosts, credentials, recording policy and motion
    settings are deliberately absent.
    """

    id: str
    name: str
    kind: CameraKind
    status: CameraStatus
    has_substream: bool
    ptz_supported: bool
    ptz_zoom_supported: bool
    stream: CameraStreamInfo

    @classmethod
    def from_camera(cls, camera: Camera) -> ViewerCameraResponse:
        return cls(
            id=camera.id,
            name=camera.name,
            kind=camera.kind,
            status=camera.status,
            has_substream=bool(camera.sub_stream_url),
            ptz_supported=camera.ptz_supported is True,
            ptz_zoom_supported=camera.ptz_supported is True and camera.ptz_zoom_supported,
            stream=CameraStreamInfo(
                codec=camera.codec,
                width=camera.width,
                height=camera.height,
                fps=camera.fps,
                audio_codec=camera.audio_codec,
                bitrate_kbps=camera.bitrate_kbps,
            ),
        )


class ViewerExportCreate(BaseModel):
    """A viewer may create a permanent clip, but cannot weaken retention.

    There is deliberately no ``protected`` field here. Every viewer export is
    permanent by construction, and only an administrator may later delete it
    or return it to automatic expiry.
    """

    model_config = ConfigDict(extra="forbid")

    camera_id: str
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at.")
        return self
