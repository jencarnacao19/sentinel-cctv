"""Discovery business logic.

The protocol modules (``wsdiscovery``, ``onvif``, ``scanner``) are kept pure --
no database, no FastAPI -- so each is testable against a real socket without
any of the surrounding web-app machinery. This module is the seam between
them and the rest of Sentinel: it turns a caller-supplied host into a
validated one before anything touches the network, translates protocol
errors into messages an operator can act on, and is the only place in this
package that talks to the database (``import_cameras``, which reuses
``app.cameras.service.create_camera`` rather than building a second way to
save a camera).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.cameras.schemas import CameraCreate
from app.cameras.service import CameraError, create_camera
from app.database.models import Camera
from app.discovery import netsafety, onvif, scanner, wsdiscovery
from app.discovery.schemas import ImportFailure
from app.logging_config import get_logger

logger = get_logger("DISCOVERY")


class DiscoveryError(Exception):
    """A discovery operation could not complete. The message is safe to show
    a user directly -- see the individual protocol modules for why."""


async def scan_network(*, timeout_seconds: float) -> wsdiscovery.DiscoveryResult:
    """WS-Discovery never touches a caller-supplied address -- it only ever
    sends to the fixed multicast group -- so no host validation belongs here;
    see ``app.discovery.netsafety`` for why every *other* function in this
    module needs it."""
    return await wsdiscovery.discover(timeout_seconds=timeout_seconds)


async def fetch_onvif_profiles(
    *,
    xaddr: str,
    username: str | None,
    password: str | None,
    timeout_seconds: float,
) -> list[onvif.MediaProfile]:
    """Resolve real stream URLs for a device's ONVIF control address.

    ``xaddr`` is never trustworthy on its own: it either came out of an
    unauthenticated UDP multicast reply, or was typed directly into this
    endpoint by an API caller who could put anything there. Either way it
    gets the same private-address check as a host typed into the known-vendor
    or NVR-scan forms before Sentinel makes a single request to it.
    """
    host = urlsplit(xaddr).hostname
    if not host:
        raise DiscoveryError("That address does not look like a valid URL.")
    try:
        await netsafety.ensure_onvif_host(host)
    except netsafety.UnscannableHostError as exc:
        raise DiscoveryError(str(exc)) from exc

    try:
        return await onvif.fetch_streams(
            device_xaddr=xaddr,
            discovered_host=host,
            username=username,
            password=password,
            timeout_seconds=timeout_seconds,
        )
    except onvif.OnvifError as exc:
        raise DiscoveryError(str(exc)) from exc


async def probe_known(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    vendor: str | None,
    timeout_seconds: float,
) -> list[scanner.CandidateResult]:
    try:
        return await scanner.probe_known_patterns(
            host=host,
            port=port,
            username=username,
            password=password,
            vendor=vendor,
            timeout_seconds=timeout_seconds,
        )
    except netsafety.UnscannableHostError as exc:
        raise DiscoveryError(str(exc)) from exc


async def scan_nvr(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    vendor: str,
    sub: bool,
    start_channel: int,
    max_channels: int,
    max_consecutive_failures: int,
    timeout_seconds: float,
) -> scanner.NvrScanResult:
    try:
        return await scanner.scan_nvr_channels(
            host=host,
            port=port,
            username=username,
            password=password,
            vendor=vendor,
            sub=sub,
            start_channel=start_channel,
            max_channels=max_channels,
            max_consecutive_failures=max_consecutive_failures,
            timeout_seconds=timeout_seconds,
        )
    except (netsafety.UnscannableHostError, ValueError) as exc:
        raise DiscoveryError(str(exc)) from exc


async def import_cameras(
    session: AsyncSession, payloads: list[CameraCreate]
) -> tuple[list[Camera], list[ImportFailure]]:
    """Create every camera in ``payloads``, continuing past individual
    failures.

    An NVR channel scan or a multi-device WS-Discovery scan naturally produces
    a batch; one entry colliding with an existing camera name (the operator
    re-importing a scan they already acted on, most often) must not cost the
    other, unrelated entries in the same batch.
    """
    created: list[Camera] = []
    failed: list[ImportFailure] = []
    for payload in payloads:
        try:
            camera = await create_camera(session, payload)
        except CameraError as exc:
            failed.append(ImportFailure(name=payload.name, error=str(exc)))
            continue
        created.append(camera)

    if failed:
        logger.info(
            "Discovery import completed with failures",
            extra={"created": len(created), "failed": len(failed)},
        )
    return created, failed
