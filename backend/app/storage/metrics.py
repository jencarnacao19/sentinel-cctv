"""Read-only recording telemetry shared by the admin and viewer APIs.

The database is the authoritative index for footage.  Deriving rates from its
recent completed segments is both cheaper and more useful than walking the
recording volume: it excludes unrelated files and lets the interface estimate
how much history the current camera bitrates will fit on the disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Camera, ExportJob, ExportStatus, RecordingSegment, SegmentStatus

RATE_WINDOW_HOURS = 6
MIN_RATE_WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class CameraStorageTelemetry:
    camera_id: str
    camera_name: str
    bytes: int
    segment_count: int
    protected_bytes: int
    oldest_recording: datetime | None
    newest_recording: datetime | None
    recording_rate_bytes_per_hour: float | None


@dataclass(frozen=True, slots=True)
class RecordingTelemetry:
    recordings_bytes: int
    segment_count: int
    protected_count: int
    protected_bytes: int
    saved_clips_count: int
    saved_clips_bytes: int
    damaged_count: int
    oldest_recording: datetime | None
    newest_recording: datetime | None
    recording_rate_bytes_per_hour: float | None
    rate_window_hours: float | None
    cameras: tuple[CameraStorageTelemetry, ...]


def _rate(
    total_bytes: int | None,
    oldest: datetime | None,
    newest: datetime | None,
) -> tuple[float | None, float | None]:
    if not total_bytes or oldest is None or newest is None:
        return None, None
    seconds = max((newest - oldest).total_seconds(), MIN_RATE_WINDOW_SECONDS)
    hours = seconds / 3600
    return float(total_bytes) / hours, hours


async def collect_recording_telemetry(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    include_cameras: bool = False,
) -> RecordingTelemetry:
    """Collect storage totals and a smoothed recent ingest rate.

    The six-hour rate window is long enough to avoid one large keyframe making
    the estimate jump, while still reacting on the same day when a camera's
    bitrate is changed. Missing rows are excluded because their files no
    longer occupy the recording volume.
    """

    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(hours=RATE_WINDOW_HOURS)
    present = RecordingSegment.status != SegmentStatus.MISSING

    summary = await session.execute(
        select(
            func.coalesce(func.sum(RecordingSegment.size_bytes), 0),
            func.count(RecordingSegment.id),
            func.coalesce(
                func.sum(
                    case(
                        (RecordingSegment.protected.is_(True), RecordingSegment.size_bytes),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(case((RecordingSegment.protected.is_(True), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((RecordingSegment.status == SegmentStatus.DAMAGED, 1), else_=0)),
                0,
            ),
            func.min(RecordingSegment.started_at),
            func.max(func.coalesce(RecordingSegment.ended_at, RecordingSegment.started_at)),
        ).where(present)
    )
    (
        recordings_bytes,
        segment_count,
        protected_bytes,
        protected_count,
        damaged_count,
        oldest,
        newest,
    ) = summary.one()

    recent = await session.execute(
        select(
            func.coalesce(func.sum(RecordingSegment.size_bytes), 0),
            func.min(RecordingSegment.started_at),
            func.max(func.coalesce(RecordingSegment.ended_at, RecordingSegment.started_at)),
        ).where(
            RecordingSegment.status == SegmentStatus.COMPLETE,
            RecordingSegment.ended_at.is_not(None),
            RecordingSegment.ended_at <= moment,
            RecordingSegment.started_at >= cutoff,
        )
    )
    recent_bytes, recent_oldest, recent_newest = recent.one()
    rate, rate_hours = _rate(int(recent_bytes or 0), recent_oldest, recent_newest)

    saved_clips = await session.execute(
        select(
            func.count(ExportJob.id),
            func.coalesce(func.sum(ExportJob.size_bytes), 0),
        ).where(
            ExportJob.status == ExportStatus.COMPLETE,
            ExportJob.protected.is_(True),
        )
    )
    saved_clips_count, saved_clips_bytes = saved_clips.one()

    cameras: tuple[CameraStorageTelemetry, ...] = ()
    if include_cameras:
        totals = await session.execute(
            select(
                Camera.id,
                Camera.name,
                func.count(RecordingSegment.id),
                func.coalesce(func.sum(RecordingSegment.size_bytes), 0),
                func.coalesce(
                    func.sum(
                        case(
                            (RecordingSegment.protected.is_(True), RecordingSegment.size_bytes),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.min(RecordingSegment.started_at),
                func.max(func.coalesce(RecordingSegment.ended_at, RecordingSegment.started_at)),
            )
            .outerjoin(
                RecordingSegment,
                and_(RecordingSegment.camera_id == Camera.id, present),
            )
            .group_by(Camera.id, Camera.name)
            .order_by(func.coalesce(func.sum(RecordingSegment.size_bytes), 0).desc())
        )

        recent_by_camera = await session.execute(
            select(
                RecordingSegment.camera_id,
                func.coalesce(func.sum(RecordingSegment.size_bytes), 0),
                func.min(RecordingSegment.started_at),
                func.max(func.coalesce(RecordingSegment.ended_at, RecordingSegment.started_at)),
            )
            .where(
                RecordingSegment.status == SegmentStatus.COMPLETE,
                RecordingSegment.ended_at.is_not(None),
                RecordingSegment.ended_at <= moment,
                RecordingSegment.started_at >= cutoff,
            )
            .group_by(RecordingSegment.camera_id)
        )
        camera_rates = {
            camera_id: _rate(int(total or 0), first, last)[0]
            for camera_id, total, first, last in recent_by_camera.all()
        }

        cameras = tuple(
            CameraStorageTelemetry(
                camera_id=camera_id,
                camera_name=name,
                bytes=int(total_bytes or 0),
                segment_count=int(count or 0),
                protected_bytes=int(saved_bytes or 0),
                oldest_recording=camera_oldest,
                newest_recording=camera_newest,
                recording_rate_bytes_per_hour=camera_rates.get(camera_id),
            )
            for (
                camera_id,
                name,
                count,
                total_bytes,
                saved_bytes,
                camera_oldest,
                camera_newest,
            ) in totals.all()
        )

    return RecordingTelemetry(
        recordings_bytes=int(recordings_bytes or 0),
        segment_count=int(segment_count or 0),
        protected_count=int(protected_count or 0),
        protected_bytes=int(protected_bytes or 0),
        saved_clips_count=int(saved_clips_count or 0),
        saved_clips_bytes=int(saved_clips_bytes or 0),
        damaged_count=int(damaged_count or 0),
        oldest_recording=oldest,
        newest_recording=newest,
        recording_rate_bytes_per_hour=rate,
        rate_window_hours=rate_hours,
        cameras=cameras,
    )


def estimate_retention_days(
    *,
    recordings_bytes: int,
    free_bytes: int,
    min_free_bytes: int | None,
    recording_rate_bytes_per_hour: float | None,
    protected_bytes: int = 0,
    max_bytes: int | None = None,
) -> tuple[float | None, float | None]:
    """Return projected total history and days remaining at the current rate."""

    if not recording_rate_bytes_per_hour or recording_rate_bytes_per_hour <= 0:
        return None, None
    usable_free = max(free_bytes - int(min_free_bytes or 0), 0)
    capacity = max(recordings_bytes + free_bytes - int(min_free_bytes or 0), 0)
    if max_bytes is not None:
        capacity = min(capacity, max_bytes)
        usable_free = min(usable_free, max(max_bytes - recordings_bytes, 0))
    capacity = max(capacity - protected_bytes, 0)
    daily = recording_rate_bytes_per_hour * 24
    return capacity / daily, usable_free / daily
