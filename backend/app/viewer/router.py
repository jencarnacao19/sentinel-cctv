"""Viewer endpoints: live, PTZ, playback and permanent clip export.

The viewer grant is intentionally an *operator* role, not an administrator: it
can consume footage, operate detected PTZ cameras and preserve a selected
range, but it cannot discover, add, edit or remove cameras; change retention;
manage users or tokens; or delete/unprotect a saved clip.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import client_key
from app.auth.ratelimit import expensive_limiter, ptz_limiter
from app.cameras import ptz, service
from app.cameras.schemas import PtzMoveRequest
from app.config import Settings, get_settings
from app.database.models import Camera, ExportJob, RecordingSegment
from app.database.session import get_session
from app.recording import export, playback, timeline
from app.recording import router as recording_router
from app.recording import timeline_router as recording_timeline_router
from app.recording.manager import get_manager as get_recording_manager
from app.recording.timeline_router import (
    ExportJobResponse,
    ExportListResponse,
    ManifestItemResponse,
    PlaybackManifestResponse,
    PlaybackTargetResponse,
    TimelineResponse,
)
from app.reliability.monitor import get_monitor as get_reliability_monitor
from app.reliability.schemas import ViewerReliabilityResponse, viewer_response
from app.storage.metrics import collect_recording_telemetry, estimate_retention_days
from app.storage.root import check_storage, get_storage_root
from app.system import health, privacy, settings_store
from app.viewer.auth import VIEWER_COOKIE_NAME, CurrentViewer, authorise_viewer
from app.viewer.schemas import ViewerCameraResponse, ViewerExportCreate

router = APIRouter(prefix="/viewer", tags=["viewer"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
MAX_PAGE_SIZE = 200
MAX_VIEWER_KEY_BYTES = 512


class ViewerSystemStatus(BaseModel):
    """Operational facts safe for an authenticated viewer.

    Paths, hosts, credentials, camera URLs and mutable policy controls are
    intentionally absent. This is an operator summary, not a diagnostics dump.
    """

    status: str
    generated_at: datetime
    cameras_total: int
    cameras_online: int
    recorders_expected: int
    recorders_running: int
    motion_expected: int
    motion_running: int
    storage_available: bool
    storage_total_bytes: int
    storage_used_bytes: int
    storage_free_bytes: int
    storage_used_percent: float
    recordings_bytes: int
    saved_clips_count: int
    saved_clips_bytes: int
    oldest_recording: datetime | None
    newest_recording: datetime | None
    recording_rate_bytes_per_hour: float | None
    configured_retention_days: int | None
    estimated_retention_days: float | None
    estimated_days_remaining: float | None
    privacy_enabled: bool
    privacy_reason: privacy.PrivacyReason | None
    privacy_message: str | None


async def _load_enabled_camera(session: AsyncSession, camera_id: str) -> Camera:
    camera = await session.get(Camera, camera_id)
    if camera is None or not camera.enabled:
        # Do not reveal whether a disabled/private camera exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found.")
    return camera


async def _load_viewer_job(session: AsyncSession, job_id: str) -> ExportJob:
    job = await session.get(ExportJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found.")
    await _load_enabled_camera(session, job.camera_id)
    return job


def _viewer_segment_url(segment_id: str) -> str:
    return f"/api/v1/viewer/recordings/{segment_id}/download"


def _viewer_job_response(job: ExportJob) -> ExportJobResponse:
    response = ExportJobResponse.from_job(job)
    if response.download_url is not None:
        response.download_url = f"/api/v1/viewer/exports/{job.id}/download"
    return response


def _bad_range(exc: timeline.TimelineRangeError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


async def _guard_export(request: Request) -> None:
    """A shared viewer must not be able to fill storage with queued jobs."""
    limit = await expensive_limiter.check(f"viewer-export:{client_key(request)}")
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many clip exports in a short time. Try again shortly.",
            headers={"Retry-After": str(int(limit.retry_after) + 1)},
        )


async def _guard_ptz(request: Request, camera_id: str) -> None:
    limit = await ptz_limiter.check(f"viewer-ptz:{client_key(request)}:{camera_id}")
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many PTZ commands in a short time.",
            headers={"Retry-After": str(max(1, int(limit.retry_after) + 1))},
        )


def _ptz_unavailable(exc: ptz.PtzError) -> HTTPException:
    code = (
        status.HTTP_409_CONFLICT
        if isinstance(exc, ptz.PtzNotConfiguredError)
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return HTTPException(status_code=code, detail=str(exc))


@router.post(
    "/session",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Start an embedded read-only viewer session",
)
async def create_viewer_session(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Move the embedded key from a one-time request into an HttpOnly cookie.

    Cloudflare-hosted viewers need no cookie. Administrators already have a
    session. The Home Assistant embed sends the key as a small ``text/plain``
    body. Unlike a custom header, that is a CORS-safelisted request and is not
    rejected by Cloudflare Access during an OPTIONS preflight. The legacy
    header remains accepted for compatible clients. Only the shared-key path
    receives this separate, session-scoped viewer cookie, keeping the key out
    of URLs, WebSocket URLs and proxy access logs.
    """
    supplied_key: str | None = None
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type == "text/plain":
        raw_key = await request.body()
        if len(raw_key) > MAX_VIEWER_KEY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Viewer key is too large.",
            )
        try:
            supplied_key = raw_key.decode("utf-8").strip() or None
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Viewer key is not valid UTF-8.",
            ) from exc

    access = await authorise_viewer(request, session, settings, supplied_key=supplied_key)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    cookie_samesite: Literal["none", "lax"] = "none" if settings.cookie_secure else "lax"
    if access.kind == "viewer_key" and settings.viewer_key is not None:
        response.set_cookie(
            key=VIEWER_COOKIE_NAME,
            value=settings.viewer_key.get_secret_value(),
            httponly=True,
            secure=settings.cookie_secure,
            # The Home Assistant panel embeds the configured viewer hostname cross-site.
            # SameSite=Lax silently drops this cookie in that iframe.
            samesite=cookie_samesite,
            path="/api/v1",
        )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/cameras", response_model=list[ViewerCameraResponse], summary="Live viewer cameras")
async def list_viewer_cameras(
    _: CurrentViewer,
    session: SessionDep,
) -> list[ViewerCameraResponse]:
    cameras = await service.list_cameras(session, enabled_only=True)
    return [ViewerCameraResponse.from_camera(camera) for camera in cameras]


@router.post(
    "/cameras/{camera_id}/ptz/move",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Move a detected PTZ camera as a footage operator",
)
async def move_viewer_camera_ptz(
    camera_id: str,
    payload: PtzMoveRequest,
    _: CurrentViewer,
    session: SessionDep,
    request: Request,
) -> Response:
    privacy.require_camera_access()
    await _guard_ptz(request, camera_id)
    camera = await _load_enabled_camera(session, camera_id)
    try:
        await ptz.get_manager().move(camera, pan=payload.pan, tilt=payload.tilt, zoom=payload.zoom)
    except ptz.PtzError as exc:
        raise _ptz_unavailable(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/cameras/{camera_id}/ptz/stop",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop every PTZ axis as a footage operator",
)
async def stop_viewer_camera_ptz(
    camera_id: str,
    _: CurrentViewer,
    session: SessionDep,
) -> Response:
    camera = await _load_enabled_camera(session, camera_id)
    try:
        await ptz.get_manager().stop_camera(camera)
    except ptz.PtzError as exc:
        raise _ptz_unavailable(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/system/status",
    response_model=ViewerSystemStatus,
    summary="Safe operator health and storage summary",
)
async def viewer_system_status(
    _: CurrentViewer,
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
) -> ViewerSystemStatus:
    cameras = await service.list_cameras(session, enabled_only=True)
    root = await get_storage_root(session, settings)
    storage = await asyncio.to_thread(check_storage, root)
    telemetry = await collect_recording_telemetry(session)
    values = await settings_store.get_many(
        session,
        [settings_store.STORAGE_RETENTION_DAYS, settings_store.STORAGE_MIN_FREE_BYTES],
    )
    projected_total, projected_remaining = estimate_retention_days(
        recordings_bytes=telemetry.recordings_bytes,
        free_bytes=storage.free_bytes,
        min_free_bytes=values[settings_store.STORAGE_MIN_FREE_BYTES],
        recording_rate_bytes_per_hour=telemetry.recording_rate_bytes_per_hour,
    )
    overall, components = await health.collect()
    recording_data = components.get("recording")
    motion_data = components.get("motion")
    recording_values = recording_data.data if recording_data is not None else {}
    motion_values = motion_data.data if motion_data is not None else {}
    used_percent = (
        min(max((storage.used_bytes / storage.total_bytes) * 100, 0), 100)
        if storage.total_bytes > 0
        else 0
    )

    response.headers["Cache-Control"] = "private, no-store"
    return ViewerSystemStatus(
        status=overall,
        generated_at=datetime.now(UTC),
        cameras_total=len(cameras),
        cameras_online=(
            0
            if privacy.get_manager().state.enabled
            else sum(camera.status.value == "online" for camera in cameras)
        ),
        recorders_expected=int(recording_values.get("expected", 0)),
        recorders_running=int(recording_values.get("running", 0)),
        motion_expected=int(motion_values.get("expected", 0)),
        motion_running=int(motion_values.get("running", 0)),
        storage_available=storage.available,
        storage_total_bytes=storage.total_bytes,
        storage_used_bytes=storage.used_bytes,
        storage_free_bytes=storage.free_bytes,
        storage_used_percent=used_percent,
        recordings_bytes=telemetry.recordings_bytes,
        saved_clips_count=telemetry.saved_clips_count,
        saved_clips_bytes=telemetry.saved_clips_bytes,
        oldest_recording=telemetry.oldest_recording,
        newest_recording=telemetry.newest_recording,
        recording_rate_bytes_per_hour=telemetry.recording_rate_bytes_per_hour,
        configured_retention_days=values[settings_store.STORAGE_RETENTION_DAYS],
        estimated_retention_days=projected_total,
        estimated_days_remaining=projected_remaining,
        privacy_enabled=privacy.get_manager().state.enabled,
        privacy_reason=privacy.get_manager().state.reason,
        privacy_message=privacy.get_manager().state.message,
    )


@router.get(
    "/reliability",
    response_model=ViewerReliabilityResponse,
    summary="Safe operator view of per-camera reliability",
)
async def viewer_reliability(
    _: CurrentViewer,
    session: SessionDep,
    response: Response,
) -> ViewerReliabilityResponse:
    """Deliberately narrower than the administrator view.

    An operator needs to know whether a camera is recording and, if not,
    roughly why. Recorder process state, restart counts, the input in use, the
    classified stream fault and the incident history all stay administrator-only,
    because none of them help someone whose job is watching footage and all of
    them describe Sentinel's internals.
    """
    snapshot = await get_reliability_monitor().snapshot(session)
    response.headers["Cache-Control"] = "private, no-store"
    return viewer_response(snapshot)


@router.get(
    "/cameras/{camera_id}/timeline",
    response_model=TimelineResponse,
    summary="Viewer playback coverage, gaps and motion",
)
async def get_viewer_timeline(
    camera_id: str,
    _: CurrentViewer,
    session: SessionDep,
    start: datetime = Query(...),
    end: datetime = Query(...),
) -> TimelineResponse:
    await _load_enabled_camera(session, camera_id)
    manager = get_recording_manager()
    try:
        result = await timeline.build_timeline(
            session,
            camera_id,
            start,
            end,
            storage_unavailable=manager.suspended_reason is not None,
        )
    except timeline.TimelineRangeError as exc:
        raise _bad_range(exc) from exc
    return TimelineResponse.from_timeline(result)


@router.get(
    "/cameras/{camera_id}/playback/manifest",
    response_model=PlaybackManifestResponse,
    summary="Viewer playback manifest",
)
async def get_viewer_playback_manifest(
    camera_id: str,
    _: CurrentViewer,
    session: SessionDep,
    start: datetime = Query(...),
    end: datetime = Query(...),
) -> PlaybackManifestResponse:
    await _load_enabled_camera(session, camera_id)
    try:
        manifest = await playback.build_manifest(session, camera_id, start, end)
    except timeline.TimelineRangeError as exc:
        raise _bad_range(exc) from exc
    response = PlaybackManifestResponse.from_manifest(manifest)
    response.items = [
        ManifestItemResponse(
            **item.model_dump(exclude={"url"}),
            url=_viewer_segment_url(item.segment_id),
        )
        for item in response.items
    ]
    return response


@router.get(
    "/cameras/{camera_id}/playback/seek",
    response_model=PlaybackTargetResponse,
    summary="Resolve a viewer playback instant",
)
async def seek_viewer_playback(
    camera_id: str,
    _: CurrentViewer,
    session: SessionDep,
    at: datetime = Query(...),
) -> PlaybackTargetResponse:
    await _load_enabled_camera(session, camera_id)
    target = PlaybackTargetResponse.from_target(await playback.resolve_seek(session, camera_id, at))
    target.url = _viewer_segment_url(target.segment_id) if target.segment_id else None
    target.next_url = (
        _viewer_segment_url(target.next_segment_id) if target.next_segment_id else None
    )
    return target


@router.get(
    "/recordings/{segment_id}/download",
    summary="Read a recording segment through the viewer boundary",
    response_class=FileResponse,
    responses={200: {"content": {"video/mp4": {}}}},
)
async def download_viewer_segment(
    segment_id: str,
    access: CurrentViewer,
    session: SessionDep,
) -> FileResponse:
    segment = await session.get(RecordingSegment, segment_id)
    if segment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found.")
    await _load_enabled_camera(session, segment.camera_id)
    # Reuse the single path-confinement and range-response implementation.
    return await recording_router.download_segment(segment_id, access, session)  # type: ignore[arg-type]


@router.post(
    "/exports",
    response_model=ExportJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a viewer clip permanently",
)
async def create_viewer_export(
    payload: ViewerExportCreate,
    _: CurrentViewer,
    session: SessionDep,
    request: Request,
) -> ExportJobResponse:
    await _guard_export(request)
    await _load_enabled_camera(session, payload.camera_id)
    try:
        job = await export.create_job(
            session,
            payload.camera_id,
            payload.start_at,
            payload.end_at,
            protected=True,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found."
        ) from exc
    except timeline.TimelineRangeError as exc:
        raise _bad_range(exc) from exc
    await session.commit()
    export.get_manager().notify()
    return _viewer_job_response(job)


@router.get(
    "/exports",
    response_model=ExportListResponse,
    summary="List saved viewer clips for one enabled camera",
)
async def list_viewer_exports(
    _: CurrentViewer,
    session: SessionDep,
    camera_id: str = Query(...),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> ExportListResponse:
    await _load_enabled_camera(session, camera_id)
    jobs, total = await export.list_jobs(session, camera_id=camera_id, limit=limit, offset=offset)
    return ExportListResponse(
        exports=[_viewer_job_response(job) for job in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/exports/{job_id}",
    response_model=ExportJobResponse,
    summary="Get one saved viewer clip",
)
async def get_viewer_export(
    job_id: str, _: CurrentViewer, session: SessionDep
) -> ExportJobResponse:
    return _viewer_job_response(await _load_viewer_job(session, job_id))


@router.get(
    "/exports/{job_id}/download",
    summary="Download a saved viewer clip",
    response_class=FileResponse,
    responses={200: {"content": {"video/mp4": {}}}},
)
async def download_viewer_export(
    job_id: str,
    access: CurrentViewer,
    session: SessionDep,
) -> FileResponse:
    await _load_viewer_job(session, job_id)
    return await recording_timeline_router.download_export(
        job_id,
        access,  # type: ignore[arg-type]
        session,
    )
