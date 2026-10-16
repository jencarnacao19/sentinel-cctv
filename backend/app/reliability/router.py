"""Administrator endpoints for the Camera Reliability Center."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AdminPrincipal, client_key
from app.auth.ratelimit import recovery_limiter
from app.database.models import Camera
from app.database.session import get_session
from app.recording.manager import get_manager as get_recording_manager
from app.recording.source_policy import REASON_LABELS as SOURCE_REASON_LABELS
from app.reliability import service
from app.reliability.monitor import RecoveryThrottledError, get_monitor
from app.reliability.schemas import (
    CameraRecoveryResponse,
    IncidentListResponse,
    ReliabilityResponse,
    SourceResetResponse,
    incident_response,
    recovery_response,
    snapshot_response,
)
from app.system import privacy

router = APIRouter(prefix="/reliability", tags=["reliability"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

MAX_PAGE_SIZE = 200

#: Wording chosen so nobody reads a queued restart as recovered video. The
#: recorder has to reconnect, FFmpeg has to negotiate the stream and a segment
#: has to close before "recording is fresh" can be true again.
RETRY_MESSAGE = (
    "Camera services were asked to restart. Recording is not confirmed until a new "
    "segment completes, which normally takes a minute or two."
)
RETRY_NOTHING_MESSAGE = (
    "Nothing could be restarted for this camera. Check that recording is configured "
    "and that the recording volume is available."
)


@router.get(
    "/cameras",
    response_model=ReliabilityResponse,
    summary="Per-camera operational state, freshness and incidents",
)
async def camera_reliability(_: AdminPrincipal, session: SessionDep) -> ReliabilityResponse:
    """Derived entirely from telemetry Sentinel already holds.

    Reading this endpoint opens no connection to any camera and writes nothing:
    incident bookkeeping belongs to the background evaluation, so refreshing a
    browser tab cannot shift a debounce window.
    """
    snapshot = await get_monitor().snapshot(session)
    return snapshot_response(snapshot)


@router.get(
    "/incidents",
    response_model=IncidentListResponse,
    summary="Recent camera incidents, active or recovered",
)
async def list_incidents(
    _: AdminPrincipal,
    session: SessionDep,
    camera_id: str | None = None,
    active_only: bool = False,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> IncidentListResponse:
    incidents, total = await service.list_incidents(
        session,
        camera_id=camera_id,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return IncidentListResponse(
        incidents=[incident_response(incident) for incident in incidents],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/cameras/{camera_id}/retry",
    response_model=CameraRecoveryResponse,
    summary="Restart one camera's local services",
)
async def retry_camera_services(
    camera_id: str,
    _: AdminPrincipal,
    session: SessionDep,
    request: Request,
) -> CameraRecoveryResponse:
    """The one manual recovery action, scoped to a single camera.

    It restarts local Sentinel processes and reloads that camera's relay
    stream. It never reboots the camera, never contacts a vendor service and
    never touches credentials or configuration. Sentinel's own supervisors keep
    retrying regardless; this exists for the case where an administrator wants
    to stop waiting for the backoff ladder.
    """
    # Refused, not queued: privacy mode means no process may reach a camera,
    # and quietly accepting the request would misrepresent that.
    privacy.require_camera_access()

    # Keyed by client only, deliberately not by camera. Per-camera protection
    # is the monitor's own cooldown; this ceiling is what stops one client from
    # cycling every camera in the house in a loop.
    limit = await recovery_limiter.check(f"recovery:{client_key(request)}")
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many recovery attempts in a short time. Try again shortly.",
            headers={"Retry-After": str(max(1, int(limit.retry_after) + 1))},
        )

    camera = await session.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found.")
    if not camera.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This camera is disabled. Enable it before retrying its services.",
        )

    try:
        outcome = await get_monitor().retry_camera(camera)
    except RecoveryThrottledError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="This camera was retried moments ago. Give it time to reconnect.",
            headers={"Retry-After": str(max(1, int(exc.retry_after) + 1))},
        ) from exc

    return recovery_response(
        outcome,
        message=RETRY_MESSAGE if outcome.actions else RETRY_NOTHING_MESSAGE,
    )


@router.post(
    "/cameras/{camera_id}/source/requalify",
    response_model=SourceResetResponse,
    summary="Clear a camera's learned recorder source and requalify",
)
async def requalify_camera_source(
    camera_id: str,
    _: AdminPrincipal,
    session: SessionDep,
    request: Request,
) -> SourceResetResponse:
    """Forget which input this camera learned to avoid, and start again.

    The policy penalises a source that fails qualification with a cooldown that
    doubles up to an hour. That is right when a camera is genuinely broken and
    wrong the moment an administrator has just fixed the network, replaced the
    camera or restarted the relay -- this is the escape hatch for that case.

    It does not restart anything by itself: the decision takes effect on the
    recorder's next start, so pressing it never interrupts footage that is
    currently being written.
    """
    privacy.require_camera_access()

    limit = await recovery_limiter.check(f"requalify:{client_key(request)}")
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many source resets in a short time. Try again shortly.",
            headers={"Retry-After": str(max(1, int(limit.retry_after) + 1))},
        )

    camera = await session.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found.")

    try:
        reason = await get_recording_manager().reset_source(camera_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found."
        ) from exc

    return SourceResetResponse(
        camera_id=camera_id,
        reason=reason,
        reason_label=SOURCE_REASON_LABELS[reason],
        message=(
            "The learned source was cleared. The camera returns to the shared local relay "
            "the next time its recorder starts, which is not interrupted now."
        ),
    )
