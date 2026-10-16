"""Administrator endpoints for local Tapo camera control.

Every route here is administrator-only, rate limited, and blocked by Sentinel's
global privacy mode before it reaches the network -- a privacy pause means no
process may contact a camera, and a vendor control is a camera contact like any
other.

The two routes with physical consequences, the siren and the reboot, additionally
require an explicit confirmation in the body. That is not ceremony: a siren is
audible in someone's home and a reboot drops the camera off every stream for a
minute, and neither should ever be one mis-click away.

Nothing here can reach a host the operator did not configure. The target is
built inside the capability layer from ``camera.host``, which is itself parsed
from the stored stream URL, and is validated as a private/local address before
any connection is attempted. No route accepts a host, a URL or a port.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import (
    AdminCameraControlPrincipal,
    AdminPrincipal,
    IntegrationReadPrincipal,
    client_key,
)
from app.auth.ratelimit import expensive_limiter
from app.cameras import service
from app.cameras.tapo import (
    WRITEABLE_FEATURES,
    TapoAuthError,
    TapoError,
    TapoUnsupportedError,
    get_manager,
)
from app.cameras.tapo_schemas import (
    TapoActionResponse,
    TapoCapabilitiesResponse,
    TapoFeatureRequest,
    TapoRebootRequest,
    TapoSirenRequest,
    TapoStateResponse,
    capabilities_response,
    state_response,
)
from app.cameras.vendor import classify_tapo_controls
from app.database.models import Camera
from app.database.session import get_session
from app.logging_config import get_logger
from app.system import privacy

router = APIRouter(prefix="/cameras", tags=["tapo"])
logger = get_logger("CAMERA")

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _guard(request: Request, camera_id: str) -> None:
    """Privacy first, then the shared expensive-operation ceiling.

    Privacy is checked before the limiter so a paused installation answers
    ``423`` rather than spending the caller's budget to tell them the same
    thing.
    """
    privacy.require_camera_access()
    limit = await expensive_limiter.check(f"tapo:{client_key(request)}")
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many camera control requests in a short time. Try again shortly.",
            headers={"Retry-After": str(max(1, int(limit.retry_after) + 1))},
        )
    del camera_id  # kept in the signature so every route reads the same way


async def _load(session: AsyncSession, camera_id: str) -> Camera:
    try:
        camera = await service.get_camera(session, camera_id)
    except service.CameraNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found."
        ) from exc
    if not camera.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This camera is disabled.")
    classification = classify_tapo_controls(camera)
    if not classification.supported:
        # Enforced here and not only in the interface. Probing another vendor's
        # camera on a private control port is not harmless: it is an
        # unauthenticated connection attempt against a device that never
        # advertised the protocol, and on an NVR channel it is one connection
        # attempt per channel against the same box.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=classification.reason,
        )
    return camera


def _translate(exc: TapoError) -> HTTPException:
    """Map a vendor failure onto the contract the PTZ routes already use."""
    if isinstance(exc, TapoUnsupportedError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, TapoAuthError):
        # The camera answered and refused. That is a configuration problem
        # upstream of Sentinel, not Sentinel being unavailable.
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get(
    "/{camera_id}/tapo/capabilities",
    response_model=TapoCapabilitiesResponse,
    summary="What this individual camera confirmed it supports",
)
async def read_capabilities(
    camera_id: str,
    _: IntegrationReadPrincipal,
    session: SessionDep,
    request: Request,
    refresh: bool = False,
) -> TapoCapabilitiesResponse:
    """Discovery, cached and persisted.

    ``refresh=true`` forces a fresh probe; without it a cached answer is
    returned, because discovery is a real round trip to a small embedded HTTP
    server and polling it on every page load is exactly how those servers fall
    over.
    """
    await _guard(request, camera_id)
    camera = await _load(session, camera_id)
    try:
        capabilities = await get_manager().capabilities(session, camera, refresh=refresh)
    except TapoError as exc:
        raise _translate(exc) from exc
    return capabilities_response(capabilities)


@router.get(
    "/{camera_id}/tapo/state",
    response_model=TapoStateResponse,
    summary="Current local state of every supported control",
)
async def read_state(
    camera_id: str,
    _: IntegrationReadPrincipal,
    session: SessionDep,
    request: Request,
    refresh: bool = False,
) -> TapoStateResponse:
    await _guard(request, camera_id)
    camera = await _load(session, camera_id)
    try:
        state = await get_manager().state(session, camera, refresh=refresh)
    except TapoError as exc:
        raise _translate(exc) from exc
    return state_response(state)


# Declared before the generic single-segment route below: FastAPI matches in
# declaration order, and `/{feature}` would otherwise swallow `/reboot`.
@router.post(
    "/{camera_id}/tapo/siren/stop",
    response_model=TapoActionResponse,
    summary="Stop a sounding siren",
)
async def stop_siren(
    camera_id: str,
    payload: TapoSirenRequest,
    _: AdminCameraControlPrincipal,
    session: SessionDep,
    request: Request,
) -> TapoActionResponse:
    return await _siren(camera_id, payload, session, request, active=False)


@router.post(
    "/{camera_id}/tapo/siren/start",
    response_model=TapoActionResponse,
    summary="Sound the camera siren",
)
async def start_siren(
    camera_id: str,
    payload: TapoSirenRequest,
    _: AdminCameraControlPrincipal,
    session: SessionDep,
    request: Request,
) -> TapoActionResponse:
    return await _siren(camera_id, payload, session, request, active=True)


async def _siren(
    camera_id: str,
    payload: TapoSirenRequest,
    session: AsyncSession,
    request: Request,
    *,
    active: bool,
) -> TapoActionResponse:
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Confirm the siren explicitly: it is audible in the building.",
        )
    await _guard(request, camera_id)
    camera = await _load(session, camera_id)
    try:
        await get_manager().set_siren(session, camera, active=active)
    except TapoError as exc:
        raise _translate(exc) from exc

    # Audited at warning level: a siren is a physical event someone in the
    # building experienced, and the log is the only record of who caused it.
    logger.warning(
        "Camera siren %s by an administrator",
        "started" if active else "stopped",
        extra={"camera_id": camera.id},
    )
    return TapoActionResponse(
        camera_id=camera.id,
        feature="siren",
        applied=True,
        message="The siren was started." if active else "The siren was stopped.",
    )


@router.post(
    "/{camera_id}/tapo/reboot",
    response_model=TapoActionResponse,
    summary="Reboot the camera",
)
async def reboot_camera(
    camera_id: str,
    payload: TapoRebootRequest,
    _: AdminPrincipal,
    session: SessionDep,
    request: Request,
) -> TapoActionResponse:
    """Restart the camera itself.

    Sentinel's own recorder is left completely alone: it will lose its input
    while the camera is down and reconnect through the existing supervisor
    backoff, which is the same path any camera reboot has always taken.
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Confirm the reboot explicitly: the camera stops recording while it restarts.",
        )
    await _guard(request, camera_id)
    camera = await _load(session, camera_id)
    try:
        await get_manager().reboot(session, camera)
    except TapoError as exc:
        raise _translate(exc) from exc

    logger.warning("Camera rebooted by an administrator", extra={"camera_id": camera.id})
    return TapoActionResponse(
        camera_id=camera.id,
        feature="reboot",
        applied=True,
        message=(
            "The camera was asked to restart. It will be missing from live view and recording "
            "for about a minute, and the recorder reconnects on its own."
        ),
    )


@router.post(
    "/{camera_id}/tapo/{feature}",
    response_model=TapoActionResponse,
    summary="Change one supported local camera setting",
)
async def write_feature(
    camera_id: str,
    feature: str,
    payload: TapoFeatureRequest,
    _: AdminCameraControlPrincipal,
    session: SessionDep,
    request: Request,
) -> TapoActionResponse:
    """One write, against one positively confirmed capability.

    The feature name is checked against a closed list before anything else, so
    a path segment can never become a vendor method name.
    """
    if feature not in WRITEABLE_FEATURES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That camera setting is not one Sentinel can change.",
        )
    await _guard(request, camera_id)
    camera = await _load(session, camera_id)

    manager = get_manager()
    try:
        await manager.set_feature(
            session,
            camera,
            feature,
            enabled=payload.enabled,
            sensitivity=payload.sensitivity
            if payload.sensitivity is not None
            else (payload.sensitivity_label),
            mode=payload.mode,
        )
        state = await manager.state(session, camera, refresh=True)
    except TapoError as exc:
        raise _translate(exc) from exc

    logger.info(
        "Local camera setting changed",
        extra={"camera_id": camera.id, "feature": feature},
    )
    return TapoActionResponse(
        camera_id=camera.id,
        feature=feature,
        applied=True,
        message="The camera accepted the change.",
        state=state_response(state),
    )
