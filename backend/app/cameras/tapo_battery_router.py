"""HTTP surface for event-based Tapo battery cameras."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.auth.deps import (
    AdminCameraControlPrincipal,
    AdminPrincipal,
    CurrentPrincipal,
    IntegrationReadPrincipal,
    client_key,
)
from app.auth.ratelimit import expensive_limiter
from app.cameras import service
from app.cameras.tapo_battery import (
    BatteryTarget,
    RemoteClip,
    TapoBatteryAuthError,
    TapoBatteryBusyError,
    TapoBatteryError,
    TapoBatteryUnsupportedError,
    get_manager,
)
from app.cameras.tapo_battery_schemas import (
    BatteryClipResponse,
    BatteryConnectionTestRequest,
    BatteryStatusResponse,
    BatterySyncRequest,
    BatterySyncResponse,
    TimedRecordingMaxUpdate,
    TimedRecordingOptions,
    TimedRecordingStartRequest,
    TimedRecordingStatusResponse,
)
from app.cameras.tapo_hls import MEDIA_NAME, battery_hls
from app.cameras.tapo_timed import (
    DEFAULT_MAX_MINUTES,
    DURATION_PRESETS_MINUTES,
    MAX_DURATION_MINUTES,
    MAX_MINUTES_SETTING_KEY,
    MIN_DURATION_MINUTES,
    StopReason,
    TimedRecordingBusyError,
    TimedRecordingError,
    TimedRecordingUnsupportedError,
    resolve_duration,
)
from app.cameras.tapo_timed import get_manager as get_timed_manager
from app.config import Settings, get_settings
from app.database.base import utcnow
from app.database.models import (
    Camera,
    CameraKind,
    CameraStatus,
    MotionEvent,
    RecordingSegment,
    SegmentOrigin,
    SegmentStatus,
)
from app.database.session import get_session
from app.logging_config import get_logger
from app.storage.root import check_storage, get_storage_root
from app.system import privacy, settings_store

router = APIRouter(prefix="/cameras", tags=["tapo-battery"])
logger = get_logger("CAMERA")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _enabled(settings: Settings) -> None:
    if not settings.enable_tapo_battery_provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The Tapo battery-camera provider is disabled.",
        )


async def _guard(request: Request, settings: Settings) -> None:
    _enabled(settings)
    privacy.require_camera_access()
    limit = await expensive_limiter.check(f"tapo-battery:{client_key(request)}")
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many battery-camera operations in a short time. Try again shortly.",
            headers={"Retry-After": str(max(1, int(limit.retry_after) + 1))},
        )


async def _load(session: AsyncSession, camera_id: str) -> Camera:
    try:
        camera = await service.get_camera(session, camera_id)
    except service.CameraNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found."
        ) from exc
    if camera.kind is not CameraKind.TAPO_BATTERY:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This is not a Tapo battery camera.",
        )
    if not camera.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This camera is disabled.")
    return camera


def _translate(exc: TapoBatteryError) -> HTTPException:
    if isinstance(exc, (TapoBatteryUnsupportedError, TapoBatteryBusyError)):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, TapoBatteryAuthError):
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        code = status.HTTP_424_FAILED_DEPENDENCY
    return HTTPException(status_code=code, detail=str(exc))


def _normalise_range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    upper = end or datetime.now(UTC)
    lower = start or (upper - timedelta(hours=24))
    if lower.tzinfo is None:
        lower = lower.replace(tzinfo=UTC)
    if upper.tzinfo is None:
        upper = upper.replace(tzinfo=UTC)
    if upper <= lower or upper - lower > timedelta(days=7):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The clip range must be positive and no longer than seven days.",
        )
    return lower.astimezone(UTC), upper.astimezone(UTC)


async def _existing_segments(
    session: AsyncSession, camera_id: str, clips: list[RemoteClip]
) -> dict[str, str]:
    external_ids = [clip.external_id for clip in clips]
    if not external_ids:
        return {}
    result = await session.execute(
        select(RecordingSegment.external_id, RecordingSegment.id).where(
            RecordingSegment.camera_id == camera_id,
            RecordingSegment.external_id.in_(external_ids),
        )
    )
    return {str(external_id): str(segment_id) for external_id, segment_id in result if external_id}


@router.post(
    "/tapo-battery/test",
    response_model=BatteryStatusResponse,
    summary="Prove local TC82/H200 authentication and battery telemetry",
)
async def test_connection(
    payload: BatteryConnectionTestRequest,
    _: AdminPrincipal,
    request: Request,
    settings: SettingsDep,
) -> BatteryStatusResponse:
    await _guard(request, settings)
    probe_id = f"probe:{payload.host}:{payload.child_id or '-'}"
    target = BatteryTarget(
        camera_id=probe_id,
        host=payload.host,
        password=payload.password,
        child_id=payload.child_id or None,
        username=payload.username or None,
        hub=payload.hub,
    )
    manager = get_manager()
    try:
        value = await manager.test_connection(target)
    except TapoBatteryError as exc:
        raise _translate(exc) from exc
    finally:
        await manager.close(probe_id)
    return BatteryStatusResponse.from_status(value)


@router.get(
    "/{camera_id}/tapo-battery/status",
    response_model=BatteryStatusResponse,
    summary="Read local battery-camera state",
)
async def read_status(
    camera_id: str,
    _: CurrentPrincipal,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
) -> BatteryStatusResponse:
    await _guard(request, settings)
    camera = await _load(session, camera_id)
    try:
        value = await get_manager().status(camera)
    except TapoBatteryError as exc:
        camera.status = CameraStatus.ERROR
        camera.status_message = str(exc)
        await session.commit()
        raise _translate(exc) from exc
    if camera.tapo_child_id:
        camera.status = CameraStatus.ONLINE
        camera.status_message = None
        camera.last_seen_at = utcnow()
    else:
        # A standalone TC82 normally sleeps. The status endpoint intentionally
        # returns topology metadata without touching the camera or port 443.
        camera.status = CameraStatus.OFFLINE
        camera.status_message = "Low-power standby; live video opens on demand."
    await session.commit()
    return BatteryStatusResponse.from_status(value)


@router.get(
    "/{camera_id}/tapo-battery/clips",
    response_model=list[BatteryClipResponse],
    summary="List event clips available from the camera or local hub",
)
async def list_clips(
    camera_id: str,
    _: CurrentPrincipal,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
) -> list[BatteryClipResponse]:
    await _guard(request, settings)
    camera = await _load(session, camera_id)
    lower, upper = _normalise_range(start, end)
    try:
        clips = await get_manager().clips(camera, lower, upper)
    except TapoBatteryError as exc:
        raise _translate(exc) from exc
    existing = await _existing_segments(session, camera.id, clips)
    return [
        BatteryClipResponse.from_clip(
            clip,
            imported=clip.external_id in existing,
            segment_id=existing.get(clip.external_id),
        )
        for clip in clips
    ]


@router.post(
    "/{camera_id}/tapo-battery/sync",
    response_model=BatterySyncResponse,
    summary="Import recent event clips into the local Sentinel timeline",
)
async def sync_clips(
    camera_id: str,
    payload: BatterySyncRequest,
    _: AdminPrincipal,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
) -> BatterySyncResponse:
    await _guard(request, settings)
    camera = await _load(session, camera_id)
    upper = datetime.now(UTC)
    lower = upper - timedelta(hours=payload.hours)
    limit = min(payload.limit, settings.tapo_battery_sync_max_clips)
    try:
        clips = (await get_manager().clips(camera, lower, upper))[:limit]
    except TapoBatteryError as exc:
        raise _translate(exc) from exc

    existing = await _existing_segments(session, camera.id, clips)
    root = await get_storage_root(session, settings)
    storage = check_storage(root)
    if not storage.available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recording storage is not available for imported clips.",
        )

    imported_ids: list[str] = []
    failed = 0
    for clip in clips:
        if clip.external_id in existing:
            continue
        relative = (
            Path(camera.id)
            / "events"
            / f"{clip.started_at:%Y}"
            / f"{clip.started_at:%m}"
            / f"{clip.started_at:%d}"
            / f"tapo-{int(clip.started_at.timestamp())}-{int(clip.ended_at.timestamp())}.mp4"
        )
        output = root / relative
        try:
            downloaded = await get_manager().download(camera, clip, output)
        except TapoBatteryError:
            failed += 1
            logger.warning(
                "Tapo event clip import failed",
                extra={"camera": camera.name, "camera_id": camera.id},
            )
            continue

        segment = RecordingSegment(
            camera_id=camera.id,
            path=relative.as_posix(),
            origin=SegmentOrigin.TAPO_EVENT,
            external_id=clip.external_id,
            started_at=clip.started_at,
            ended_at=clip.ended_at,
            duration_seconds=clip.duration_seconds,
            size_bytes=downloaded.path.stat().st_size,
            codec=downloaded.probe.codec,
            width=downloaded.probe.width,
            height=downloaded.probe.height,
            fps=downloaded.probe.fps,
            has_motion=True,
            motion_seconds=clip.duration_seconds,
            protected=False,
            status=SegmentStatus.COMPLETE,
        )
        event = MotionEvent(
            camera_id=camera.id,
            started_at=clip.started_at,
            ended_at=clip.ended_at,
            duration_seconds=clip.duration_seconds,
            peak_score=1.0,
            pre_roll_start=clip.started_at,
            post_roll_end=clip.ended_at,
            protected=False,
        )
        session.add_all([segment, event])
        await session.flush()
        imported_ids.append(segment.id)

    camera.status = CameraStatus.ONLINE
    camera.status_message = None
    camera.last_seen_at = utcnow()
    await session.commit()
    logger.info(
        "Tapo event clips synchronised",
        extra={"camera": camera.name, "camera_id": camera.id, "imported": len(imported_ids)},
    )
    return BatterySyncResponse(
        discovered=len(clips),
        imported=len(imported_ids),
        already_present=len(existing),
        failed=failed,
        segment_ids=imported_ids,
    )


@router.get(
    "/{camera_id}/tapo-battery/live",
    summary="Open one bounded on-demand local Direct Stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"video/mp2t": {}}}},
)
async def live_stream(
    camera_id: str,
    _: CurrentPrincipal,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    quality: Literal["HD", "SD"] = Query(default="HD"),
) -> StreamingResponse:
    await _guard(request, settings)
    camera = await _load(session, camera_id)
    manager = get_manager()
    try:
        await manager.reserve_live(camera.id)
    except TapoBatteryError as exc:
        raise _translate(exc) from exc

    # Authenticate and open the vendor session *before* the response starts.
    # Doing it inside the body iterator meant a rejected credential arrived
    # after "200 OK, video/mp2t" was already on the wire, so the client got a
    # successful-looking response with no body -- which a reverse proxy reports
    # as a broken origin rather than as the camera problem it is.
    try:
        live = await manager.open_live(camera, quality=quality)
    except TapoBatteryError as exc:
        await manager.release_live(camera.id)
        raise _translate(exc) from exc
    except BaseException:
        await manager.release_live(camera.id)
        raise

    chunks = live.chunks(
        max_seconds=settings.tapo_battery_live_max_seconds,
        ffmpeg_binary=settings.ffmpeg_binary,
    )
    try:
        first = await asyncio.wait_for(anext(chunks), timeout=20.0)
    except BaseException as exc:
        with suppress(Exception):
            await chunks.aclose()
            await live.close()
        await manager.release_live(camera.id)
        if isinstance(exc, TapoBatteryError):
            raise _translate(exc) from exc
        if isinstance(exc, (TimeoutError, StopAsyncIteration)):
            raise HTTPException(
                status_code=424, detail="The camera did not deliver playable video."
            ) from exc
        raise

    async def body():
        try:
            yield first
            async for chunk in chunks:
                yield chunk
        finally:
            await chunks.aclose()
            await live.close()
            await manager.release_live(camera.id)

    return StreamingResponse(
        body(),
        media_type="video/mp2t",
        background=BackgroundTask(manager.release_live, camera.id),
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Content-Disposition": "inline",
        },
    )


@router.post("/{camera_id}/tapo-battery/hls-session")
async def open_native_live(
    camera_id: str,
    principal: CurrentPrincipal,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
) -> dict[str, str]:
    await _guard(request, settings)
    camera = await _load(session, camera_id)
    try:
        token = await battery_hls.open(camera, settings, owner=f"{principal.kind}:{principal.id}")
    except TapoBatteryError as exc:
        raise _translate(exc) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=424, detail="The camera did not deliver playable video."
        ) from exc
    base = f"/api/v1/cameras/{camera_id}/tapo-battery/hls/{token}"
    return {"url": f"{base}/playlist.m3u8", "stop_url": base}


@router.get("/{camera_id}/tapo-battery/hls/{token}/{filename}", include_in_schema=False)
async def native_live_resource(
    camera_id: str, token: str, filename: str, settings: SettingsDep
) -> Response:
    _enabled(settings)
    privacy.require_camera_access()
    job = battery_hls.resolve(camera_id, token)
    if job is None or MEDIA_NAME.fullmatch(filename) is None:
        raise HTTPException(status_code=404, detail="Stream expired.")
    try:
        content = await asyncio.to_thread((job.directory / filename).read_bytes)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Stream fragment expired.") from exc
    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t",
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
    )


@router.delete("/{camera_id}/tapo-battery/hls/{token}", status_code=204)
async def stop_native_live(camera_id: str, token: str, principal: CurrentPrincipal) -> Response:
    await battery_hls.close(camera_id, token, owner=f"{principal.kind}:{principal.id}")
    return Response(status_code=204)


# --- timed manual recording ------------------------------------------------

BATTERY_DRAIN_WARNING = (
    "Recording keeps this battery camera awake for the whole period, with its radio "
    "and encoder running continuously. That is far heavier than the event-only use "
    "it is designed for and can flatten the battery completely, after which the "
    "camera stops recording and stays offline until it is charged."
)


def _translate_timed(exc: TimedRecordingError) -> HTTPException:
    if isinstance(exc, TimedRecordingBusyError | TimedRecordingUnsupportedError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


async def _configured_max_minutes(session: AsyncSession) -> int:
    stored = await settings_store.get_setting(session, MAX_MINUTES_SETTING_KEY, DEFAULT_MAX_MINUTES)
    try:
        value = int(stored)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_MINUTES
    return max(MIN_DURATION_MINUTES, min(value, MAX_DURATION_MINUTES))


def _options(ceiling: int, battery: int | None = None) -> TimedRecordingOptions:
    return TimedRecordingOptions(
        presets_minutes=[value for value in DURATION_PRESETS_MINUTES if value <= ceiling],
        max_minutes=ceiling,
        hard_max_minutes=MAX_DURATION_MINUTES,
        min_minutes=MIN_DURATION_MINUTES,
        battery_percent=battery,
        warning=BATTERY_DRAIN_WARNING,
    )


@router.get(
    "/{camera_id}/tapo-battery/timed-recording/options",
    response_model=TimedRecordingOptions,
    summary="Durations an administrator may request",
)
async def timed_recording_options(
    camera_id: str,
    _: IntegrationReadPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> TimedRecordingOptions:
    _enabled(settings)
    await _load(session, camera_id)
    ceiling = await _configured_max_minutes(session)
    # Status is fetched once by the parent battery dialog. Keeping this endpoint
    # configuration-only avoids opening two simultaneous vendor sessions merely
    # because the dialog mounted both panels.
    return _options(ceiling)


@router.put(
    "/tapo-battery/timed-recording/max-minutes",
    response_model=TimedRecordingOptions,
    summary="Set the longest timed recording this installation allows",
)
async def set_timed_recording_max(
    payload: TimedRecordingMaxUpdate,
    _: AdminPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> TimedRecordingOptions:
    _enabled(settings)
    await settings_store.set_setting(session, MAX_MINUTES_SETTING_KEY, payload.max_minutes)
    await session.commit()
    return _options(await _configured_max_minutes(session), None)


@router.get(
    "/{camera_id}/tapo-battery/timed-recording",
    response_model=TimedRecordingStatusResponse | None,
    summary="Progress of the timed recording, if one is running",
)
async def read_timed_recording(
    camera_id: str,
    _: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> TimedRecordingStatusResponse | None:
    """Deliberately cheap: in-memory state only, no camera contact.

    This is polled every second or two while a recording runs, so it must never
    open a vendor session -- that would spend the camera's radio budget on
    reporting about the camera's radio budget.
    """
    _enabled(settings)
    camera = await _load(session, camera_id)
    value = get_timed_manager().status(camera.id)
    return None if value is None else TimedRecordingStatusResponse.from_status(value)


@router.post(
    "/{camera_id}/tapo-battery/timed-recording",
    response_model=TimedRecordingStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Record this battery camera for a bounded period, on request",
)
async def start_timed_recording(
    camera_id: str,
    payload: TimedRecordingStartRequest,
    principal: AdminCameraControlPrincipal,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
) -> TimedRecordingStatusResponse:
    """Start, then return immediately; progress is read by polling.

    The request deliberately does not wait for the recording. An hour-long
    operation held open behind one HTTP call would be cut off by any reverse
    proxy long before it finished, and the operator would be shown the proxy's
    error page instead of their own recording. What this call does do
    synchronously is prove the camera answers, so a failure is reported here
    rather than as a session that looks alive and produces nothing.
    """
    await _guard(request, settings)
    camera = await _load(session, camera_id)
    if not payload.accept_battery_drain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Confirm that you accept the battery cost of keeping this camera "
                "awake for the whole period."
            ),
        )
    ceiling = await _configured_max_minutes(session)
    try:
        seconds = resolve_duration(payload.minutes, configured_max=ceiling)
    except TimedRecordingError as exc:
        raise _translate_timed(exc) from exc

    try:
        battery = (
            (await get_manager().status(camera)).battery_percent if camera.tapo_child_id else None
        )
    except TapoBatteryError as exc:
        raise _translate(exc) from exc

    try:
        value = await get_timed_manager().begin(
            camera,
            duration_seconds=seconds,
            battery_percent=battery,
            requested_by=principal.name,
        )
    except TimedRecordingError as exc:
        raise _translate_timed(exc) from exc
    except TapoBatteryError as exc:
        raise _translate(exc) from exc
    return TimedRecordingStatusResponse.from_status(value)


@router.delete(
    "/{camera_id}/tapo-battery/timed-recording",
    response_model=TimedRecordingStatusResponse,
    summary="Stop a timed recording now and keep what was captured",
)
async def stop_timed_recording(
    camera_id: str,
    _: AdminCameraControlPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> TimedRecordingStatusResponse:
    _enabled(settings)
    camera = await _load(session, camera_id)
    try:
        value = await get_timed_manager().request_stop(camera.id, StopReason.OPERATOR)
    except TimedRecordingError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return TimedRecordingStatusResponse.from_status(value)
