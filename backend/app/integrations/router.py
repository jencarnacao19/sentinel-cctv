"""Home Assistant integration handshake endpoint.

The one new REST endpoint this phase adds -- see ``CONTRACT.md``. Everything
else Home Assistant needs (the camera list, snapshots, storage, motion
events) already exists and needed no change to be usable with an API token;
this endpoint adds only a stable identity, a protocol version, and the two
per-camera facts (``recording_state``, ``motion_active``) that no existing
endpoint computes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.auth.deps import CurrentPrincipal
from app.cameras.service import list_cameras
from app.database.models import Camera, CameraStatus, MotionEvent, RecordingMode
from app.database.session import get_session
from app.integrations.identity import install_id
from app.integrations.schemas import (
    API_VERSION,
    HaCamera,
    HaCameraCapabilities,
    HaCameraStream,
    HaHandshake,
)
from app.recording.manager import get_manager as get_recording_manager

router = APIRouter(prefix="/integrations", tags=["home-assistant"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

#: Full path including the API version prefix, exactly as documented in
#: CONTRACT.md section 5 -- the integration turns this into a ws(s):// URL
#: verbatim, so it is never shortened to a path relative to this router.
WEBSOCKET_PATH = "/api/v1/events/stream"


async def _cameras_with_open_motion(session: AsyncSession) -> set[str]:
    """Camera ids with a `MotionEvent` still in progress right now.

    This is the state a `binary_sensor` should show before the first
    `motion.started` frame ever reaches it over the socket -- without this,
    a Home Assistant restart during an ongoing event would show "clear"
    until the event happens to end.
    """
    rows = await session.execute(
        select(MotionEvent.camera_id).where(MotionEvent.ended_at.is_(None)).distinct()
    )
    return {camera_id for (camera_id,) in rows.all()}


def _recording_state(camera: Camera, *, storage_suspended: bool) -> str:
    """One of exactly ``RECORDING_STATES`` -- see CONTRACT.md section 3.

    Continuous is the only mode that actually records anything today
    (PROJECT_STATUS, "Known issues"); motion-only and scheduled are reported
    idle rather than recording, which would claim a camera is being recorded
    when nothing is happening to it.
    """
    if not camera.enabled or camera.recording_mode is RecordingMode.OFF:
        return "off"
    if storage_suspended or camera.status not in (CameraStatus.ONLINE, CameraStatus.CONNECTING):
        return "unavailable"
    if camera.recording_mode is RecordingMode.CONTINUOUS:
        return "recording"
    return "idle"


def _capabilities(camera: Camera) -> HaCameraCapabilities:
    has_stream = bool(camera.main_stream_url)
    return HaCameraCapabilities(
        snapshot=camera.enabled and has_stream,
        live_stream=camera.enabled and has_stream,
        recording=camera.enabled and camera.recording_mode is RecordingMode.CONTINUOUS,
        motion_events=camera.motion_enabled,
    )


def _to_ha_camera(camera: Camera, *, motion_active: bool, storage_suspended: bool) -> HaCamera:
    return HaCamera(
        id=camera.id,
        name=camera.name,
        enabled=camera.enabled,
        status=camera.status,
        manufacturer=camera.manufacturer,
        model=camera.model,
        recording_mode=camera.recording_mode,
        recording_state=_recording_state(camera, storage_suspended=storage_suspended),
        motion_enabled=camera.motion_enabled,
        motion_active=motion_active,
        capabilities=_capabilities(camera),
        stream=HaCameraStream(
            codec=camera.codec,
            width=camera.width,
            height=camera.height,
            fps=camera.fps,
            audio_codec=camera.audio_codec,
            bitrate_kbps=camera.bitrate_kbps,
        ),
    )


@router.get(
    "/home-assistant",
    response_model=HaHandshake,
    summary="Home Assistant integration handshake",
)
async def home_assistant_handshake(
    _: CurrentPrincipal, session: Annotated[AsyncSession, Depends(get_session)]
) -> HaHandshake:
    """Identity, protocol version and camera list, in one authenticated call.

    Doubles as credential validation for the integration's config flow: a
    revoked or malformed token gets the same 401 every other endpoint would
    give it, which is exactly how the integration notices and asks Home
    Assistant to reauthenticate.
    """
    cameras = await list_cameras(session)
    motion_active_ids = await _cameras_with_open_motion(session)
    storage_suspended = get_recording_manager().suspended_reason is not None

    return HaHandshake(
        unique_id=install_id(),
        version=__version__,
        api_version=API_VERSION,
        websocket_path=WEBSOCKET_PATH,
        cameras=[
            _to_ha_camera(
                camera,
                motion_active=camera.id in motion_active_ids,
                storage_suspended=storage_suspended,
            )
            for camera in cameras
        ],
    )
