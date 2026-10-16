"""Recording endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AdminPrincipal, CurrentPrincipal
from app.config import Settings, get_settings
from app.database.models import RecordingSegment, SegmentStatus
from app.database.session import get_session
from app.logging_config import get_logger
from app.recording import paths
from app.recording.manager import get_manager
from app.recording.schemas import (
    RecorderStatusResponse,
    SegmentListResponse,
    SegmentResponse,
    SegmentUpdate,
)
from app.security.paths import UnsafePathError, resolve_within
from app.storage.root import get_storage_root

router = APIRouter(prefix="/recordings", tags=["recordings"])
logger = get_logger("RECORDING")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

MAX_PAGE_SIZE = 500


@router.get("", response_model=SegmentListResponse, summary="List recording segments")
async def list_segments(
    _: CurrentPrincipal,
    session: SessionDep,
    camera_id: str | None = None,
    start: datetime | None = Query(default=None, description="Inclusive UTC lower bound"),
    end: datetime | None = Query(default=None, description="Exclusive UTC upper bound"),
    protected: bool | None = None,
    has_motion: bool | None = None,
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> SegmentListResponse:
    filters = [RecordingSegment.status != SegmentStatus.MISSING]
    if camera_id:
        filters.append(RecordingSegment.camera_id == camera_id)
    if start is not None:
        # A segment overlaps the window when it ends after `start`; comparing
        # started_at alone would drop the segment already in progress.
        filters.append((RecordingSegment.ended_at.is_(None)) | (RecordingSegment.ended_at > start))
    if end is not None:
        filters.append(RecordingSegment.started_at < end)
    if protected is not None:
        filters.append(RecordingSegment.protected.is_(protected))
    if has_motion is not None:
        filters.append(RecordingSegment.has_motion.is_(has_motion))

    total = await session.scalar(select(func.count()).select_from(RecordingSegment).where(*filters))
    rows = await session.execute(
        select(RecordingSegment)
        .where(*filters)
        .order_by(RecordingSegment.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return SegmentListResponse(
        segments=[SegmentResponse.from_segment(row) for row in rows.scalars()],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get("/status", response_model=RecorderStatusResponse, summary="Recorder status")
async def recorder_status(_: CurrentPrincipal, session: SessionDep) -> RecorderStatusResponse:
    manager = get_manager()
    storage = manager.storage
    root = await get_storage_root(session)
    return RecorderStatusResponse(
        storage_path=str(root),
        storage_available=bool(storage and storage.available),
        suspended_reason=manager.suspended_reason,
        total_bytes=storage.total_bytes if storage else 0,
        free_bytes=storage.free_bytes if storage else 0,
        recorders=manager.recorder_status(),
    )


async def _load(session: AsyncSession, segment_id: str) -> RecordingSegment:
    segment = await session.get(RecordingSegment, segment_id)
    if segment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found.")
    return segment


@router.get("/{segment_id}", response_model=SegmentResponse, summary="Get one segment")
async def get_segment(segment_id: str, _: CurrentPrincipal, session: SessionDep) -> SegmentResponse:
    return SegmentResponse.from_segment(await _load(session, segment_id))


@router.patch(
    "/{segment_id}", response_model=SegmentResponse, summary="Protect or unprotect a segment"
)
async def update_segment(
    segment_id: str, payload: SegmentUpdate, _: AdminPrincipal, session: SessionDep
) -> SegmentResponse:
    segment = await _load(session, segment_id)
    if payload.protected is not None:
        segment.protected = payload.protected
        logger.info(
            "Segment protection changed",
            extra={"segment_id": segment.id, "protected": payload.protected},
        )
    await session.commit()
    return SegmentResponse.from_segment(segment)


@router.get(
    "/{segment_id}/download",
    summary="Download a segment",
    response_class=FileResponse,
    responses={200: {"content": {"video/mp4": {}}}},
)
async def download_segment(
    segment_id: str, _: CurrentPrincipal, session: SessionDep
) -> FileResponse:
    segment = await _load(session, segment_id)
    if segment.status is SegmentStatus.MISSING:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="This recording is no longer on disk."
        )

    root = await get_storage_root(session)
    try:
        # The stored path came from our own indexer, but it is still resolved
        # against the storage root: a database restored from elsewhere, or a
        # symlink planted in the recordings tree, must not be able to serve an
        # arbitrary file.
        absolute = resolve_within(paths.absolute_path(root, segment.path), root)
    except (UnsafePathError, OSError) as exc:
        logger.error("Recording path escapes the storage root", extra={"segment_id": segment.id})
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found."
        ) from exc

    if not absolute.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="This recording is no longer on disk."
        )

    stamp = segment.started_at.strftime("%Y%m%d-%H%M%S")
    return FileResponse(
        absolute,
        media_type="video/mp4",
        filename=f"sentinel-{stamp}.mp4",
        # Range requests are what make seeking work in the browser player.
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"},
    )
