"""Camera endpoints."""

from __future__ import annotations

from collections.abc import Collection
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import (
    AdminCameraControlPrincipal,
    AdminPrincipal,
    CameraControlPrincipal,
    CurrentPrincipal,
    client_key,
)
from app.auth.ratelimit import expensive_limiter, ptz_limiter
from app.cameras import ptz, service
from app.cameras import tapo as tapo_control
from app.cameras.schemas import (
    CameraCreate,
    CameraIdentityResponse,
    CameraRecordingRequest,
    CameraRecordingResponse,
    CameraResponse,
    CameraTestResponse,
    CameraUpdate,
    PtzCapabilityResponse,
    PtzMoveRequest,
    PtzPatrolRequest,
    PtzPatrolResponse,
    PtzPresetCreateRequest,
    PtzPresetResponse,
    PtzSetupResponse,
    StreamTestRequest,
    StreamTestResponse,
    TapoControlAvailability,
)
from app.cameras.snapshot import SnapshotError, capture_snapshot
from app.database.base import utcnow
from app.database.models import Camera, CameraKind
from app.database.session import get_session
from app.logging_config import get_logger
from app.motion.manager import get_manager as get_motion_manager
from app.recording.manager import get_manager as get_recording_manager
from app.streaming.client import Go2RtcUnavailableError
from app.streaming.config import build_streams, stream_names
from app.streaming.manager import get_manager
from app.system import privacy

router = APIRouter(prefix="/cameras", tags=["cameras"])
logger = get_logger("CAMERA")

SessionDep = Annotated[AsyncSession, Depends(get_session)]

SNAPSHOT_MAX_AGE_SECONDS = 2


async def _guard_expensive(request: Request) -> None:
    """Probing and snapshots spawn an FFmpeg process and touch the network."""
    privacy.require_camera_access()
    limit = await expensive_limiter.check(f"camera:{client_key(request)}")
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many stream tests in a short time. Try again shortly.",
            headers={"Retry-After": str(int(limit.retry_after) + 1)},
        )


async def _guard_ptz(request: Request, camera_id: str) -> None:
    privacy.require_camera_access()
    limit = await ptz_limiter.check(f"ptz:{client_key(request)}:{camera_id}")
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


async def _snapshot_via_go2rtc(camera: Camera) -> bytes | None:
    """Try go2rtc first. Returns None when it cannot serve the frame."""
    manager = get_manager()
    if not manager.is_ready:
        return None
    main, sub = stream_names(camera)
    try:
        return await manager.client.snapshot(sub or main)
    except Go2RtcUnavailableError:
        logger.debug("go2rtc snapshot unavailable", extra={"camera": camera.name})
        return None


async def _sync_streams(
    *,
    replace_streams: Collection[str] = (),
    restart_recorder_id: str | None = None,
    restart_motion_id: str | None = None,
) -> None:
    """Push camera changes to go2rtc, recorders and motion workers.

    Failures are logged and swallowed: the camera record is already saved, the
    generated config on disk means the next go2rtc start picks it up, and each
    runtime manager can recover independently.
    """
    try:
        await get_manager().sync(replace_streams=replace_streams)
    except Exception:
        logger.warning("Could not synchronise streams with go2rtc", exc_info=True)

    try:
        recording_manager = get_recording_manager()
        if restart_recorder_id:
            await recording_manager.restart_camera(restart_recorder_id)
        else:
            await recording_manager.sync()
    except Exception:
        logger.warning("Could not synchronise recorders", exc_info=True)

    try:
        motion_manager = get_motion_manager()
        if restart_motion_id:
            await motion_manager.restart_camera(restart_motion_id)
        else:
            await motion_manager.sync()
    except Exception:
        logger.warning("Could not synchronise motion workers", exc_info=True)


async def sync_camera_runtime() -> None:
    """Bring go2rtc, recorders and motion workers in line with the camera list.

    For callers outside this router that change cameras in bulk, such as a
    configuration import.
    """
    await _sync_streams()


async def _load(session: AsyncSession, camera_id: str) -> Camera:
    try:
        return await service.get_camera(session, camera_id)
    except service.CameraNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found."
        ) from exc


@router.get("", response_model=list[CameraResponse], summary="List cameras")
async def list_cameras(
    _: CurrentPrincipal, session: SessionDep, enabled_only: bool = False
) -> list[CameraResponse]:
    cameras = await service.list_cameras(session, enabled_only=enabled_only)
    return [CameraResponse.from_camera(camera) for camera in cameras]


@router.post(
    "",
    response_model=CameraResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a camera",
)
async def create_camera(
    payload: CameraCreate, _: AdminPrincipal, session: SessionDep
) -> CameraResponse:
    try:
        camera = await service.create_camera(session, payload)
    except service.DuplicateCameraNameError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except service.InvalidCameraError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    # Release SQLite's write transaction before contacting the camera. A slow
    # ONVIF stack must not block recorder/event writes while a camera is added.
    await session.commit()
    if camera.onvif_device_xaddr and not privacy.get_manager().state.enabled:
        try:
            capability = await ptz.get_manager().detect(camera)
            camera.onvif_device_xaddr = capability.device_xaddr
            camera.onvif_ptz_xaddr = capability.ptz_xaddr
            camera.onvif_profile_token = capability.profile_token or camera.onvif_profile_token
            camera.ptz_supported = capability.supported
            camera.ptz_zoom_supported = capability.zoom_supported
            camera.ptz_status_message = capability.message
            camera.ptz_last_checked_at = utcnow()
        except ptz.PtzError as exc:
            camera.ptz_supported = None
            camera.ptz_zoom_supported = False
            camera.ptz_status_message = str(exc)
            camera.ptz_last_checked_at = utcnow()
        await session.commit()
    await _sync_streams()
    return CameraResponse.from_camera(camera)


@router.post(
    "/test-stream",
    response_model=StreamTestResponse,
    summary="Test a stream URL before saving it",
)
async def test_stream(
    payload: StreamTestRequest,
    _: AdminPrincipal,
    request: Request,
) -> StreamTestResponse:
    await _guard_expensive(request)
    return await service.test_stream(payload.url, payload.username, payload.password)


@router.get("/{camera_id}", response_model=CameraResponse, summary="Get a camera")
async def get_camera(camera_id: str, _: CurrentPrincipal, session: SessionDep) -> CameraResponse:
    return CameraResponse.from_camera(await _load(session, camera_id))


@router.patch("/{camera_id}", response_model=CameraResponse, summary="Update a camera")
async def update_camera(
    camera_id: str,
    payload: CameraUpdate,
    principal: AdminCameraControlPrincipal,
    session: SessionDep,
) -> CameraResponse:
    # Older HA clients use the general PATCH for their recording-mode switch.
    # Inspect fields explicitly supplied, including nulls, before loading or writing.
    if principal.is_machine and payload.model_fields_set - {"recording_mode"}:
        raise HTTPException(
            status_code=403,
            detail="Integration tokens can update only recording_mode here. "
            "Use the camera recording endpoint to turn recording on or off.",
        )
    camera = await _load(session, camera_id)
    previous_streams = build_streams([camera])
    previous_recording_profile = camera.recording_profile
    previous_source_mode = camera.recording_source_mode
    previous_segment_seconds = camera.recording_segment_seconds
    previous_recording_enabled = camera.recording_enabled
    tapo_identity_changed = bool(
        payload.model_fields_set
        & {
            "host",
            "main_stream_url",
            "username",
            "password",
            "tapo_password",
            "tapo_child_id",
        }
    )
    try:
        updated = await service.update_camera(session, camera, payload)
    except service.DuplicateCameraNameError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except service.InvalidCameraError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    updated_streams = build_streams([updated])
    replaced_streams = {
        name for name, sources in updated_streams.items() if previous_streams.get(name) != sources
    }
    stream_configuration_changed = previous_streams != updated_streams
    recording_profile_changed = previous_recording_profile is not updated.recording_profile
    source_mode_changed = previous_source_mode is not updated.recording_source_mode
    segment_seconds_changed = previous_segment_seconds != updated.recording_segment_seconds
    recording_enabled_changed = previous_recording_enabled != updated.recording_enabled
    await session.commit()
    if recording_enabled_changed:
        # Configure and the row toggle are two doors into the same setting, so
        # both have to move the actual recorder.
        await _apply_recording(updated)
    if tapo_identity_changed:
        # Drop the authenticated LAN session immediately when its target or
        # credentials change. The manager would reject the old fingerprint on
        # the next call as well, but retaining a session for credentials the
        # administrator explicitly removed is unnecessary and surprising.
        await tapo_control.get_manager().close(updated.id)
    await _sync_streams(
        replace_streams=replaced_streams,
        restart_recorder_id=(
            updated.id
            if updated.recording_enabled
            and (
                stream_configuration_changed
                or recording_profile_changed
                or source_mode_changed
                or segment_seconds_changed
            )
            else None
        ),
        restart_motion_id=updated.id,
    )
    return CameraResponse.from_camera(updated)


_RECORDING_MESSAGES: dict[str, str] = {
    "recording": "This camera is recording.",
    "off": "Recording is off for this camera.",
    "paused": "Recording is paused: privacy mode or storage is holding every recorder.",
    "failed": "Sentinel could not start this camera's recorder. Check the camera and storage.",
    "unsupported": "This camera does not use Sentinel's continuous recorder.",
}


async def _apply_recording(camera: Camera) -> str:
    """Move the real recorder, then report what actually happened."""
    manager = get_recording_manager()
    try:
        await manager.apply_camera_recording(camera)
    except Exception:
        # Logged, not surfaced: the underlying text can name a stream URL. The
        # caller gets the observed state instead, which is the honest answer.
        logger.exception(
            "Applying a recording change failed",
            extra={"camera": camera.name, "camera_id": camera.id},
        )
    return manager.camera_recording_state(camera)


@router.put(
    "/{camera_id}/recording",
    response_model=CameraRecordingResponse,
    summary="Turn one camera's recorder on or off",
)
async def set_camera_recording(
    camera_id: str,
    payload: CameraRecordingRequest,
    _: AdminCameraControlPrincipal,
    session: SessionDep,
) -> CameraRecordingResponse:
    """One camera's recorder, and nothing else.

    Live view, ONVIF, motion detection, the other cameras and global privacy
    mode are all untouched. Stopping requires an explicit confirmation because
    the footage that is not recorded cannot be recovered later.
    """
    camera = await _load(session, camera_id)
    if camera.kind is not CameraKind.RTSP:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This camera does not use Sentinel's continuous recorder.",
        )
    if not payload.enabled and not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Confirm stopping this camera's recording explicitly: "
                "footage from the paused period cannot be recovered."
            ),
        )
    if camera.recording_enabled != payload.enabled:
        camera.recording_enabled = payload.enabled
        camera.updated_at = utcnow()
        await session.commit()
        logger.info(
            "Camera recording %s by an authorised operator",
            "enabled" if payload.enabled else "disabled",
            extra={"camera": camera.name, "camera_id": camera.id},
        )
    state = await _apply_recording(camera)
    if not payload.enabled and get_recording_manager().is_recording(camera.id):
        # The requested privacy boundary was not achieved. Restore the stored
        # intent so the database never claims this recorder is off while its
        # process is still alive, then return an actionable, sanitised error.
        camera.recording_enabled = True
        camera.updated_at = utcnow()
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Sentinel could not stop this camera's recorder. Recording is still active; "
                "try again or use global Privacy mode."
            ),
        )
    return CameraRecordingResponse(
        camera_id=camera.id,
        recording_enabled=camera.recording_enabled,
        state=state,
        message=_RECORDING_MESSAGES[state],
    )


@router.post(
    "/{camera_id}/identify",
    response_model=CameraIdentityResponse,
    summary="Ask the camera over ONVIF which device it is",
)
async def identify_camera(
    camera_id: str,
    _: AdminPrincipal,
    session: SessionDep,
    request: Request,
) -> CameraIdentityResponse:
    """One read-only ONVIF call, so brand-specific controls stop being guesswork.

    Nothing on the camera changes. The manufacturer and model it reports are
    stored, which is what lets Sentinel decide on its own whether the Tapo LAN
    controls apply.
    """
    await _guard_expensive(request)
    camera = await _load(session, camera_id)
    if camera.kind is not CameraKind.RTSP:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This camera is not reachable over ONVIF.",
        )
    try:
        identity = await ptz.get_manager().identify(camera)
    except ptz.PtzError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    if identity.manufacturer:
        camera.manufacturer = identity.manufacturer
    if identity.model:
        camera.model = identity.model
    if identity.manufacturer or identity.model:
        camera.updated_at = utcnow()
        await session.commit()

    named = ", ".join(part for part in (identity.manufacturer, identity.model) if part)
    return CameraIdentityResponse(
        camera_id=camera.id,
        manufacturer=camera.manufacturer,
        model=camera.model,
        firmware_version=identity.firmware_version,
        tapo_controls=TapoControlAvailability.from_camera(camera),
        message=(
            f"The camera identifies itself as {named}."
            if named
            else "The camera answered but did not name its manufacturer or model."
        ),
    )


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a camera")
async def delete_camera(camera_id: str, _: AdminPrincipal, session: SessionDep) -> Response:
    camera = await _load(session, camera_id)
    await service.delete_camera(session, camera)
    await session.commit()
    await tapo_control.get_manager().close(camera_id)
    await _sync_streams()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{camera_id}/test",
    response_model=CameraTestResponse,
    summary="Test a saved camera's streams",
)
async def test_camera(
    camera_id: str, _: AdminCameraControlPrincipal, session: SessionDep, request: Request
) -> CameraTestResponse:
    await _guard_expensive(request)
    camera = await _load(session, camera_id)
    result = await service.test_camera(session, camera)
    await session.commit()
    return result


@router.post(
    "/{camera_id}/ptz/detect",
    response_model=PtzCapabilityResponse,
    summary="Detect and cache ONVIF PTZ support",
)
async def detect_camera_ptz(
    camera_id: str,
    _: AdminPrincipal,
    session: SessionDep,
    request: Request,
) -> PtzCapabilityResponse:
    await _guard_expensive(request)
    camera = await _load(session, camera_id)
    checked_at = utcnow()
    try:
        capability = await ptz.get_manager().detect(camera)
    except ptz.PtzError as exc:
        camera.ptz_supported = None
        camera.ptz_zoom_supported = False
        camera.ptz_status_message = str(exc)
        camera.ptz_last_checked_at = checked_at
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    camera.onvif_device_xaddr = capability.device_xaddr
    camera.onvif_ptz_xaddr = capability.ptz_xaddr
    camera.onvif_profile_token = capability.profile_token or camera.onvif_profile_token
    camera.ptz_supported = capability.supported
    camera.ptz_zoom_supported = capability.zoom_supported
    camera.ptz_status_message = capability.message
    camera.ptz_last_checked_at = checked_at
    await session.commit()
    return PtzCapabilityResponse(
        supported=capability.supported,
        zoom_supported=capability.zoom_supported,
        preset_capacity=capability.preset_capacity,
        preset_tour_capacity=capability.preset_tour_capacity,
        home_supported=capability.home_supported,
        message=capability.message,
        checked_at=checked_at,
    )


@router.get(
    "/{camera_id}/ptz/setup",
    response_model=PtzSetupResponse,
    summary="Read local ONVIF presets and patrol capabilities",
)
async def get_camera_ptz_setup(
    camera_id: str,
    _: CurrentPrincipal,
    session: SessionDep,
) -> PtzSetupResponse:
    privacy.require_camera_access()
    camera = await _load(session, camera_id)
    try:
        capabilities = await ptz.get_manager().get_node_capabilities(camera)
        presets = await ptz.get_manager().list_presets(camera)
    except ptz.PtzError as exc:
        raise _ptz_unavailable(exc) from exc
    backend = (
        "onvif_lan"
        if camera.onvif_patrol_tour_token
        else "tapo_lan"
        if camera.tapo_patrol_mode
        else "none"
    )
    return PtzSetupResponse(
        presets=[PtzPresetResponse(token=item.token, name=item.name) for item in presets],
        preset_capacity=capabilities.preset_capacity,
        preset_tour_capacity=capabilities.preset_tour_capacity,
        home_supported=capabilities.home_supported,
        patrol_backend=backend,
    )


@router.post(
    "/{camera_id}/ptz/presets",
    response_model=PtzPresetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save the current camera position as an ONVIF preset",
)
async def create_camera_ptz_preset(
    camera_id: str,
    payload: PtzPresetCreateRequest,
    _: AdminPrincipal,
    session: SessionDep,
    request: Request,
) -> PtzPresetResponse:
    await _guard_ptz(request, camera_id)
    camera = await _load(session, camera_id)
    try:
        preset = await ptz.get_manager().set_preset(camera, payload.name)
    except ptz.PtzError as exc:
        raise _ptz_unavailable(exc) from exc
    return PtzPresetResponse(token=preset.token, name=preset.name)


@router.post(
    "/{camera_id}/ptz/presets/{preset_token}/goto",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Move a camera to a local ONVIF preset",
)
async def goto_camera_ptz_preset(
    camera_id: str,
    preset_token: str,
    _: CameraControlPrincipal,
    session: SessionDep,
    request: Request,
) -> Response:
    await _guard_ptz(request, camera_id)
    camera = await _load(session, camera_id)
    try:
        await ptz.get_manager().goto_preset(camera, preset_token)
    except ptz.PtzError as exc:
        raise _ptz_unavailable(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{camera_id}/ptz/presets/{preset_token}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a local ONVIF preset",
)
async def delete_camera_ptz_preset(
    camera_id: str,
    preset_token: str,
    _: AdminPrincipal,
    session: SessionDep,
    request: Request,
) -> Response:
    await _guard_ptz(request, camera_id)
    camera = await _load(session, camera_id)
    if camera.onvif_patrol_tour_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stop the ONVIF patrol before removing one of its presets.",
        )
    try:
        await ptz.get_manager().remove_preset(camera, preset_token)
    except ptz.PtzError as exc:
        raise _ptz_unavailable(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{camera_id}/ptz/patrol",
    response_model=PtzPatrolResponse,
    summary="Configure and start a camera-owned ONVIF patrol",
)
async def start_camera_ptz_patrol(
    camera_id: str,
    payload: PtzPatrolRequest,
    _: AdminPrincipal,
    session: SessionDep,
    request: Request,
) -> PtzPatrolResponse:
    await _guard_ptz(request, camera_id)
    camera = await _load(session, camera_id)
    try:
        token = await ptz.get_manager().configure_native_patrol(
            camera,
            preset_tokens=payload.preset_tokens,
            stay_seconds=payload.stay_seconds,
            name=payload.name,
        )
    except ptz.PtzError as exc:
        raise _ptz_unavailable(exc) from exc
    camera.onvif_patrol_tour_token = token
    await session.commit()
    return PtzPatrolResponse(enabled=True, backend="onvif_lan")


@router.delete(
    "/{camera_id}/ptz/patrol",
    response_model=PtzPatrolResponse,
    summary="Stop automatic patrol without affecting manual local PTZ",
)
async def stop_camera_ptz_patrol(
    camera_id: str,
    _: AdminPrincipal,
    session: SessionDep,
    request: Request,
) -> PtzPatrolResponse:
    await _guard_ptz(request, camera_id)
    camera = await _load(session, camera_id)
    try:
        await ptz.get_manager().disable_patrol(camera)
    except ptz.PtzError as exc:
        raise _ptz_unavailable(exc) from exc
    camera.onvif_patrol_tour_token = None
    camera.tapo_patrol_mode = None
    await session.commit()
    return PtzPatrolResponse(enabled=False, backend="none")


@router.post(
    "/{camera_id}/ptz/move",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Move a PTZ camera while the control is held",
)
async def move_camera_ptz(
    camera_id: str,
    payload: PtzMoveRequest,
    _: CameraControlPrincipal,
    session: SessionDep,
    request: Request,
) -> Response:
    await _guard_ptz(request, camera_id)
    camera = await _load(session, camera_id)
    if not camera.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This camera is disabled.")
    try:
        await ptz.get_manager().move(camera, pan=payload.pan, tilt=payload.tilt, zoom=payload.zoom)
    except ptz.PtzError as exc:
        raise _ptz_unavailable(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{camera_id}/ptz/stop",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop every PTZ axis immediately",
)
async def stop_camera_ptz(
    camera_id: str,
    _: CameraControlPrincipal,
    session: SessionDep,
) -> Response:
    camera = await _load(session, camera_id)
    try:
        await ptz.get_manager().stop_camera(camera)
    except ptz.PtzError as exc:
        raise _ptz_unavailable(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{camera_id}/snapshot",
    summary="Capture a still image",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def camera_snapshot(
    camera_id: str,
    _: CurrentPrincipal,
    session: SessionDep,
    request: Request,
    width: int | None = None,
) -> Response:
    await _guard_expensive(request)
    camera = await _load(session, camera_id)

    if not camera.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This camera is disabled.")
    url = service.resolve_stream_url(camera, sub=bool(camera.sub_stream_url))
    if url is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This camera has no stream URL."
        )
    if width is not None and not 64 <= width <= 3840:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Width must be between 64 and 3840 pixels.",
        )

    # go2rtc already holds a connection to this camera, so pulling the frame
    # from it costs the camera nothing. Opening the stream directly is the
    # fallback for when go2rtc is still starting or is not managed here.
    image = await _snapshot_via_go2rtc(camera)
    if image is None:
        try:
            image = await capture_snapshot(url, width=width)
        except SnapshotError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

    return Response(
        content=image,
        media_type="image/jpeg",
        headers={"Cache-Control": f"private, max-age={SNAPSHOT_MAX_AGE_SECONDS}"},
    )
