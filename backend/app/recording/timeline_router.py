"""Timeline, playback and export endpoints.

Three related but distinct questions live here, matching the three modules
that answer them: what does a range look like (``timeline``), what should the
player append next (``playback``), and what stand-alone file should Sentinel
produce from a range (``export``). Kept in one router file because they share
almost every dependency and all describe the same "camera, time range"
surface, but each stays a thin wrapper around its own module -- this file adds
HTTP semantics and response shaping, nothing more.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AdminPrincipal, CurrentPrincipal
from app.database.models import Camera, ExportJob, ExportStatus
from app.database.session import get_session
from app.logging_config import get_logger
from app.recording import export, playback, timeline
from app.recording.export import KEYFRAME_ACCURACY_NOTE
from app.recording.manager import get_manager as get_recording_manager
from app.recording.playback import ManifestItem, PlaybackManifest
from app.recording.timeline import Gap, GapReason, MotionBand, PlaybackTarget, Timeline, TimeSpan
from app.security.paths import UnsafePathError
from app.storage.root import get_storage_root

router = APIRouter(prefix="/cameras", tags=["timeline"])
exports_router = APIRouter(prefix="/exports", tags=["exports"])
logger = get_logger("RECORDING")

SessionDep = Annotated[AsyncSession, Depends(get_session)]

MAX_PAGE_SIZE = 200


def _download_url(segment_id: str) -> str:
    """Playback never serves recording bytes itself -- it points back at the
    recordings API that already does, so there is exactly one code path that
    resolves a segment id to a file on disk."""
    return f"/api/v1/recordings/{segment_id}/download"


async def _load_camera(session: AsyncSession, camera_id: str) -> Camera:
    camera = await session.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found.")
    return camera


def _bad_range(exc: timeline.TimelineRangeError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


# --- Timeline schemas -----------------------------------------------------


class TimeSpanResponse(BaseModel):
    start: datetime
    end: datetime
    duration_seconds: float

    @classmethod
    def from_span(cls, span: TimeSpan) -> TimeSpanResponse:
        return cls(start=span.start, end=span.end, duration_seconds=span.duration_seconds)


class GapResponse(BaseModel):
    start: datetime
    end: datetime
    duration_seconds: float
    reason: GapReason

    @classmethod
    def from_gap(cls, gap: Gap) -> GapResponse:
        return cls(
            start=gap.start, end=gap.end, duration_seconds=gap.duration_seconds, reason=gap.reason
        )


class MotionBandResponse(BaseModel):
    start: datetime
    end: datetime
    peak_score: float
    event_count: int

    @classmethod
    def from_band(cls, band: MotionBand) -> MotionBandResponse:
        return cls(
            start=band.start, end=band.end, peak_score=band.peak_score, event_count=band.event_count
        )


class TimelineResponse(BaseModel):
    camera_id: str
    start: datetime
    end: datetime
    coverage: list[TimeSpanResponse]
    """Runs of playable footage. Never overlaps a gap."""
    gaps: list[GapResponse]
    motion: list[MotionBandResponse]
    recorded_seconds: float
    motion_seconds: float
    damaged_seconds: float
    gap_seconds: float
    segment_count: int
    motion_event_count: int
    bucket_seconds: float
    truncated: bool

    @classmethod
    def from_timeline(cls, result: Timeline) -> TimelineResponse:
        return cls(
            camera_id=result.camera_id,
            start=result.start,
            end=result.end,
            coverage=[TimeSpanResponse.from_span(span) for span in result.coverage],
            gaps=[GapResponse.from_gap(gap) for gap in result.gaps],
            motion=[MotionBandResponse.from_band(band) for band in result.motion],
            recorded_seconds=result.recorded_seconds,
            motion_seconds=result.motion_seconds,
            damaged_seconds=result.damaged_seconds,
            gap_seconds=result.gap_seconds,
            segment_count=result.segment_count,
            motion_event_count=result.motion_event_count,
            bucket_seconds=result.bucket_seconds,
            truncated=result.truncated,
        )


# --- Playback schemas -------------------------------------------------------


class ManifestItemResponse(BaseModel):
    segment_id: str
    started_at: datetime
    ended_at: datetime
    offset_seconds: float
    """Non-zero only for the first item: where the requested range begins
    inside that file."""
    duration_seconds: float
    url: str

    @classmethod
    def from_item(cls, item: ManifestItem) -> ManifestItemResponse:
        return cls(
            segment_id=item.segment_id,
            started_at=item.started_at,
            ended_at=item.ended_at,
            offset_seconds=item.offset_seconds,
            duration_seconds=item.duration_seconds,
            url=_download_url(item.segment_id),
        )


class PlaybackManifestResponse(BaseModel):
    camera_id: str
    start: datetime
    end: datetime
    items: list[ManifestItemResponse]
    gaps: list[TimeSpanResponse]
    """Stretches with no playable footage. The player skips to the next
    item's start rather than stalling on an empty buffer."""
    total_seconds: float
    truncated: bool
    """More footage matched than fit in one manifest; request again starting
    where this response left off."""

    @classmethod
    def from_manifest(cls, manifest: PlaybackManifest) -> PlaybackManifestResponse:
        return cls(
            camera_id=manifest.camera_id,
            start=manifest.start,
            end=manifest.end,
            items=[ManifestItemResponse.from_item(item) for item in manifest.items],
            gaps=[TimeSpanResponse.from_span(gap) for gap in manifest.gaps],
            total_seconds=manifest.total_seconds,
            truncated=manifest.truncated,
        )


class PlaybackTargetResponse(BaseModel):
    camera_id: str
    at: datetime
    found: bool
    segment_id: str | None
    url: str | None
    started_at: datetime | None
    ended_at: datetime | None
    offset_seconds: float
    next_segment_id: str | None
    next_url: str | None
    next_started_at: datetime | None
    continuous: bool
    """The next segment begins within the join tolerance, so playback can roll
    straight into it rather than jumping."""
    gap_seconds: float | None

    @classmethod
    def from_target(cls, target: PlaybackTarget) -> PlaybackTargetResponse:
        return cls(
            camera_id=target.camera_id,
            at=target.at,
            found=target.found,
            segment_id=target.segment_id,
            url=_download_url(target.segment_id) if target.segment_id else None,
            started_at=target.started_at,
            ended_at=target.ended_at,
            offset_seconds=target.offset_seconds,
            next_segment_id=target.next_segment_id,
            next_url=_download_url(target.next_segment_id) if target.next_segment_id else None,
            next_started_at=target.next_started_at,
            continuous=target.continuous,
            gap_seconds=target.gap_seconds,
        )


# --- Timeline & playback endpoints -----------------------------------------


@router.get(
    "/{camera_id}/timeline",
    response_model=TimelineResponse,
    summary="Coverage, gaps and motion across a time range",
)
async def get_timeline(
    camera_id: str,
    _: CurrentPrincipal,
    session: SessionDep,
    start: datetime = Query(..., description="Inclusive UTC lower bound"),
    end: datetime = Query(..., description="Exclusive UTC upper bound"),
) -> TimelineResponse:
    await _load_camera(session, camera_id)
    manager = get_recording_manager()
    try:
        result = await timeline.build_timeline(
            session,
            camera_id,
            start,
            end,
            # Reflects the recorder's *live* state -- see build_timeline's own
            # docstring for why only an ongoing outage can be attributed here.
            storage_unavailable=manager.suspended_reason is not None,
        )
    except timeline.TimelineRangeError as exc:
        raise _bad_range(exc) from exc
    return TimelineResponse.from_timeline(result)


@router.get(
    "/{camera_id}/playback/manifest",
    response_model=PlaybackManifestResponse,
    summary="Ordered segments to append for a time range",
)
async def get_playback_manifest(
    camera_id: str,
    _: CurrentPrincipal,
    session: SessionDep,
    start: datetime = Query(..., description="Inclusive UTC lower bound"),
    end: datetime = Query(..., description="Exclusive UTC upper bound"),
) -> PlaybackManifestResponse:
    await _load_camera(session, camera_id)
    try:
        manifest = await playback.build_manifest(session, camera_id, start, end)
    except timeline.TimelineRangeError as exc:
        raise _bad_range(exc) from exc
    return PlaybackManifestResponse.from_manifest(manifest)


@router.get(
    "/{camera_id}/playback/seek",
    response_model=PlaybackTargetResponse,
    summary="Resolve a wall-clock instant to a segment and offset",
)
async def seek_playback(
    camera_id: str,
    _: CurrentPrincipal,
    session: SessionDep,
    at: datetime = Query(..., description="UTC instant to resolve"),
) -> PlaybackTargetResponse:
    await _load_camera(session, camera_id)
    target = await playback.resolve_seek(session, camera_id, at)
    return PlaybackTargetResponse.from_target(target)


# --- Export schemas ----------------------------------------------------------


class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_id: str
    start_at: datetime
    end_at: datetime
    protected: bool = True

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at.")
        return self


class ExportJobResponse(BaseModel):
    id: str
    camera_id: str
    start_at: datetime
    end_at: datetime
    status: ExportStatus
    progress: float
    """0.0-1.0, derived from output duration against requested duration."""
    size_bytes: int | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime | None
    protected: bool
    download_url: str | None
    note: str
    """Always present: the keyframe-accuracy disclaimer that applies to every
    export, not just this one."""

    @classmethod
    def from_job(cls, job: ExportJob) -> ExportJobResponse:
        # Built explicitly rather than from the ORM object so the on-disk path
        # never reaches a client -- the same reasoning as recording segments.
        return cls(
            id=job.id,
            camera_id=job.camera_id,
            start_at=job.start_at,
            end_at=job.end_at,
            status=job.status,
            progress=job.progress,
            size_bytes=job.size_bytes,
            error=job.error,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            expires_at=job.expires_at,
            protected=job.protected,
            download_url=f"/api/v1/exports/{job.id}/download" if job.is_downloadable else None,
            note=KEYFRAME_ACCURACY_NOTE,
        )


class ExportListResponse(BaseModel):
    exports: list[ExportJobResponse]
    total: int
    limit: int
    offset: int


class ExportUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protected: bool


async def _load_job(session: AsyncSession, job_id: str) -> ExportJob:
    job = await session.get(ExportJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found.")
    return job


# --- Export endpoints --------------------------------------------------------


@exports_router.post(
    "",
    response_model=ExportJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Queue a clip export",
)
async def create_export(
    payload: ExportCreate, _: AdminPrincipal, session: SessionDep
) -> ExportJobResponse:
    try:
        job = await export.create_job(
            session,
            payload.camera_id,
            payload.start_at,
            payload.end_at,
            protected=payload.protected,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found."
        ) from exc
    except timeline.TimelineRangeError as exc:
        raise _bad_range(exc) from exc

    await session.commit()
    # Wakes the worker loop immediately rather than waiting for its next poll
    # tick; harmless (and a no-op) if the loop is not running, e.g. in tests.
    export.get_manager().notify()
    return ExportJobResponse.from_job(job)


@exports_router.get("", response_model=ExportListResponse, summary="List export jobs")
async def list_exports(
    _: CurrentPrincipal,
    session: SessionDep,
    camera_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> ExportListResponse:
    jobs, total = await export.list_jobs(session, camera_id=camera_id, limit=limit, offset=offset)
    return ExportListResponse(
        exports=[ExportJobResponse.from_job(job) for job in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


@exports_router.get("/{job_id}", response_model=ExportJobResponse, summary="Get one export job")
async def get_export(job_id: str, _: CurrentPrincipal, session: SessionDep) -> ExportJobResponse:
    return ExportJobResponse.from_job(await _load_job(session, job_id))


@exports_router.patch(
    "/{job_id}",
    response_model=ExportJobResponse,
    summary="Save a clip permanently or restore automatic expiry",
)
async def update_export(
    job_id: str,
    payload: ExportUpdate,
    _: AdminPrincipal,
    session: SessionDep,
) -> ExportJobResponse:
    job = await _load_job(session, job_id)
    export.set_protected(job, payload.protected)
    await session.commit()
    return ExportJobResponse.from_job(job)


@exports_router.get(
    "/{job_id}/download",
    summary="Download a completed export",
    response_class=FileResponse,
    responses={200: {"content": {"video/mp4": {}}}},
)
async def download_export(job_id: str, _: CurrentPrincipal, session: SessionDep) -> FileResponse:
    job = await _load_job(session, job_id)
    if job.status is not ExportStatus.COMPLETE or not job.file_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This export is not ready to download."
        )

    root = await get_storage_root(session)
    try:
        absolute = export.resolve_output_file(root, job)
    except UnsafePathError as exc:
        logger.error("Export path escapes the exports root", extra={"job_id": job.id})
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Export not found."
        ) from exc

    if not absolute.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="This export is no longer on disk."
        )

    stamp = job.start_at.strftime("%Y%m%d-%H%M%S")
    return FileResponse(
        absolute,
        media_type="video/mp4",
        filename=f"sentinel-export-{stamp}.mp4",
        # Range requests are what let a browser scrub a downloaded export
        # instead of buffering the whole file first.
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"},
    )


@exports_router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel a running export, or remove a finished one",
)
async def delete_export(job_id: str, _: AdminPrincipal, session: SessionDep) -> Response:
    deleted = await export.get_manager().delete_job(session, job_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found.")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
