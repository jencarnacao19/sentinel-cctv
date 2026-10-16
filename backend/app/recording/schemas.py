"""Request and response models for recordings."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.database.models import RecordingSegment, SegmentOrigin, SegmentStatus


class SegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    camera_id: str
    origin: SegmentOrigin
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float | None
    size_bytes: int
    codec: str | None
    width: int | None
    height: int | None
    fps: float | None
    has_motion: bool
    motion_seconds: float
    protected: bool
    status: SegmentStatus

    @classmethod
    def from_segment(cls, segment: RecordingSegment) -> SegmentResponse:
        # Built explicitly rather than from the ORM object so the on-disk path
        # never reaches a client: it is an internal detail, and exposing it
        # would invite path-based requests.
        return cls(
            id=segment.id,
            camera_id=segment.camera_id,
            origin=segment.origin,
            started_at=segment.started_at,
            ended_at=segment.ended_at,
            duration_seconds=segment.duration_seconds,
            size_bytes=segment.size_bytes,
            codec=segment.codec,
            width=segment.width,
            height=segment.height,
            fps=segment.fps,
            has_motion=segment.has_motion,
            motion_seconds=segment.motion_seconds,
            protected=segment.protected,
            status=segment.status,
        )


class SegmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protected: bool | None = None


class SegmentListResponse(BaseModel):
    segments: list[SegmentResponse]
    total: int
    limit: int
    offset: int


class RecorderStatusResponse(BaseModel):
    """What the diagnostics page and the dashboard banner need."""

    storage_path: str | None
    storage_available: bool
    suspended_reason: str | None
    total_bytes: int
    free_bytes: int
    recorders: list[dict[str, object]] = Field(default_factory=list)
