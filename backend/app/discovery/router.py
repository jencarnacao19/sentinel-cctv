"""Discovery endpoints.

Every route here either touches the network (a WS-Discovery scan, an ONVIF
call, a stream probe) or writes cameras from the result, so every route
requires an administrator -- the same requirement ``app.cameras.router``
places on adding a camera by hand. The network-touching routes additionally
share the expensive-endpoint rate limiter with camera probing: a discovery
scan is exactly the kind of request ``app.auth.ratelimit.expensive_limiter``
exists for.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AdminPrincipal, client_key
from app.auth.ratelimit import expensive_limiter
from app.cameras.schemas import CameraResponse
from app.database.session import get_session
from app.discovery import profiles, service
from app.discovery.schemas import (
    ImportCamerasRequest,
    ImportCamerasResponse,
    KnownProbeCandidateResponse,
    KnownProbeRequest,
    KnownProbeResponse,
    MediaProfileResponse,
    NvrScanRequest,
    NvrScanResponse,
    OnvifProfilesRequest,
    OnvifProfilesResponse,
    ScanRequest,
    ScanResponse,
    VendorsResponse,
)
from app.logging_config import get_logger
from app.recording.manager import get_manager as get_recording_manager
from app.streaming.manager import get_manager as get_streaming_manager
from app.system import privacy

router = APIRouter(prefix="/discovery", tags=["discovery"])
logger = get_logger("DISCOVERY")

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _guard_expensive(request: Request) -> None:
    """Every route below spawns network I/O or a subprocess per candidate URL."""
    privacy.require_camera_access()
    limit = await expensive_limiter.check(f"discovery:{client_key(request)}")
    if not limit.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many discovery requests in a short time. Try again shortly.",
            headers={"Retry-After": str(int(limit.retry_after) + 1)},
        )


async def _sync_after_import() -> None:
    """Push newly imported cameras to go2rtc and the recorders.

    Mirrors ``app.cameras.router._sync_streams``: the camera rows are already
    committed by the time this runs, so a failure here is logged and
    swallowed rather than surfaced as a failed import -- the next scheduled
    sync (or the next camera change through the ordinary CRUD endpoints)
    picks up any camera this pass could not reach.
    """
    try:
        await get_streaming_manager().sync()
    except Exception:
        logger.warning("Could not synchronise streams with go2rtc after import", exc_info=True)

    try:
        await get_recording_manager().sync()
    except Exception:
        logger.warning("Could not synchronise recorders after import", exc_info=True)


@router.get("/vendors", response_model=VendorsResponse, summary="Known vendor URL patterns")
async def vendors(_: AdminPrincipal) -> VendorsResponse:
    return VendorsResponse(
        known_stream_vendors=list(profiles.KNOWN_VENDORS),
        nvr_vendors=list(profiles.NVR_VENDORS),
    )


@router.post("/scan", response_model=ScanResponse, summary="WS-Discovery network scan")
async def scan(payload: ScanRequest, _: AdminPrincipal, request: Request) -> ScanResponse:
    await _guard_expensive(request)
    result = await service.scan_network(timeout_seconds=payload.timeout_seconds)
    return ScanResponse.from_result(result)


@router.post(
    "/profiles",
    response_model=OnvifProfilesResponse,
    summary="Resolve a discovered device's ONVIF media profiles into real stream URLs",
)
async def onvif_profiles(
    payload: OnvifProfilesRequest, _: AdminPrincipal, request: Request
) -> OnvifProfilesResponse:
    await _guard_expensive(request)
    try:
        found = await service.fetch_onvif_profiles(
            xaddr=payload.xaddr,
            username=payload.username,
            password=payload.password,
            timeout_seconds=payload.timeout_seconds,
        )
    except service.DiscoveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return OnvifProfilesResponse(profiles=[MediaProfileResponse.from_profile(p) for p in found])


@router.post(
    "/probe-known",
    response_model=KnownProbeResponse,
    summary="Try documented vendor URL patterns against a host",
)
async def probe_known(
    payload: KnownProbeRequest, _: AdminPrincipal, request: Request
) -> KnownProbeResponse:
    await _guard_expensive(request)
    try:
        candidates = await service.probe_known(
            host=payload.host,
            port=payload.port,
            username=payload.username,
            password=payload.password,
            vendor=payload.vendor,
            timeout_seconds=payload.timeout_seconds,
        )
    except service.DiscoveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return KnownProbeResponse(
        candidates=[KnownProbeCandidateResponse.from_candidate(c) for c in candidates]
    )


@router.post(
    "/scan-nvr",
    response_model=NvrScanResponse,
    summary="Probe an NVR's channels in sequence",
)
async def scan_nvr(payload: NvrScanRequest, _: AdminPrincipal, request: Request) -> NvrScanResponse:
    await _guard_expensive(request)
    try:
        result = await service.scan_nvr(
            host=payload.host,
            port=payload.port,
            username=payload.username,
            password=payload.password,
            vendor=payload.vendor,
            sub=payload.sub,
            start_channel=payload.start_channel,
            max_channels=payload.max_channels,
            max_consecutive_failures=payload.max_consecutive_failures,
            timeout_seconds=payload.timeout_seconds,
        )
    except service.DiscoveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return NvrScanResponse.from_result(result)


@router.post(
    "/import",
    response_model=ImportCamerasResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a batch of discovered cameras",
)
async def import_cameras(
    payload: ImportCamerasRequest, _: AdminPrincipal, session: SessionDep
) -> ImportCamerasResponse:
    created, failed = await service.import_cameras(session, payload.cameras)
    await session.commit()
    if created:
        await _sync_after_import()
    return ImportCamerasResponse(
        created=[CameraResponse.from_camera(camera) for camera in created], failed=failed
    )
