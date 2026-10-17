"""Camera business logic."""

from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cameras.probe import ProbeError, ProbeResult, probe_stream
from app.cameras.rtsp import (
    InvalidStreamUrlError,
    build_stream_url,
    parse_stream_url,
)
from app.cameras.schemas import (
    CameraCreate,
    CameraTestResponse,
    CameraUpdate,
    StreamTestResponse,
)
from app.cameras.vendor import TapoControlSource, classify_tapo_controls
from app.database.base import utcnow
from app.database.models import Camera, CameraKind, CameraStatus
from app.events.bus import CAMERA_ADDED, CAMERA_OFFLINE, CAMERA_ONLINE, CAMERA_REMOVED, publish
from app.logging_config import get_logger
from app.security.crypto import encrypt_secret, try_decrypt_secret

logger = get_logger("CAMERA")


class CameraError(Exception):
    """Base class for camera failures with a user-facing message."""


class DuplicateCameraNameError(CameraError):
    pass


class CameraNotFoundError(CameraError):
    pass


BATTERY_UNSUPPORTED = (
    "Tapo battery cameras are not supported in this release. They are being "
    "developed on Sentinel's dev build."
)


class InvalidCameraError(CameraError):
    pass


def _validate_onvif_host(xaddr: str | None, camera_host: str) -> str | None:
    """Keep future ONVIF calls pinned to the camera's already-approved host."""
    if not xaddr:
        return None
    hostname = urlsplit(xaddr).hostname
    if hostname is None or hostname.rstrip(".").lower() != camera_host.rstrip(".").lower():
        raise InvalidCameraError("The ONVIF service address must use the camera's stream host.")
    return xaddr


# --- Credential handling ------------------------------------------------


def _split_credentials(
    url: str, username: str | None, password: str | None
) -> tuple[str, str, str | None, str | None]:
    """Return ``(clean_url, host, username, password)``.

    Credentials given as separate fields win over any embedded in the URL: if a
    user pastes an old URL and types a new password, the new password is the one
    they meant.
    """
    try:
        parsed = parse_stream_url(url)
    except InvalidStreamUrlError as exc:
        raise InvalidCameraError(str(exc)) from exc

    return (
        parsed.sanitised_url,
        parsed.host,
        username or parsed.username,
        password or parsed.password,
    )


def resolve_stream_url(camera: Camera, *, sub: bool = False) -> str | None:
    """Rebuild a playable URL with credentials attached.

    The return value is a live secret: it belongs in an FFmpeg argument list or a
    go2rtc config file, and nowhere else.
    """
    base = camera.sub_stream_url if sub else camera.main_stream_url
    if not base:
        return None
    # Checked again here, not only when the API accepted it: this value goes
    # straight into go2rtc.yaml and FFmpeg's argument list, and a database
    # restored from elsewhere could hold anything. In go2rtc's config an
    # "exec:" source would run a command.
    try:
        parse_stream_url(base)
    except InvalidStreamUrlError:
        logger.warning(
            "Ignoring a stored stream URL that is not RTSP",
            extra={"camera_id": camera.id, "substream": sub},
        )
        return None
    return build_stream_url(base, camera.username, try_decrypt_secret(camera.encrypted_password))


# --- Queries ------------------------------------------------------------


async def list_cameras(session: AsyncSession, *, enabled_only: bool = False) -> list[Camera]:
    statement = select(Camera).order_by(Camera.name)
    if enabled_only:
        statement = statement.where(Camera.enabled.is_(True))
    result = await session.execute(statement)
    return list(result.scalars())


async def get_camera(session: AsyncSession, camera_id: str) -> Camera:
    camera = await session.get(Camera, camera_id)
    if camera is None:
        raise CameraNotFoundError(f"No camera with id {camera_id}.")
    return camera


async def count_cameras(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Camera))
    return int(result.scalar_one())


# --- Mutations ----------------------------------------------------------


async def create_camera(session: AsyncSession, payload: CameraCreate) -> Camera:
    main_url: str | None
    sub_url: str | None
    host: str
    username: str | None
    password: str | None
    onvif_xaddr: str | None
    if payload.kind is CameraKind.TAPO_BATTERY:
        raise InvalidCameraError(BATTERY_UNSUPPORTED)
    if not payload.main_stream_url:
        raise InvalidCameraError("An RTSP main stream URL is required.")
    main_url, host, username, password = _split_credentials(
        payload.main_stream_url, payload.username, payload.password
    )

    sub_url = None
    if payload.sub_stream_url:
        sub_url, _, sub_username, sub_password = _split_credentials(
            payload.sub_stream_url, payload.username, payload.password
        )
        # A substream URL usually carries the same credentials; only fall back to
        # them if the main URL had none.
        username = username or sub_username
        password = password or sub_password

    onvif_xaddr = _validate_onvif_host(payload.onvif_device_xaddr, host)

    camera = Camera(
        name=payload.name.strip(),
        kind=payload.kind,
        manufacturer=payload.manufacturer,
        model=payload.model,
        nvr_group=payload.nvr_group,
        host=host,
        main_stream_url=main_url,
        sub_stream_url=sub_url,
        username=username,
        encrypted_password=encrypt_secret(password) if password else None,
        encrypted_tapo_password=(
            encrypt_secret(payload.tapo_password) if payload.tapo_password else None
        ),
        tapo_child_id=payload.tapo_child_id or None,
        onvif_device_xaddr=onvif_xaddr,
        onvif_profile_token=payload.onvif_profile_token,
        enabled=payload.enabled,
        recording_mode=payload.recording_mode,
        recording_profile=payload.recording_profile,
        recording_source_mode=payload.recording_source_mode,
        recording_segment_seconds=payload.recording_segment_seconds,
        recording_enabled=payload.recording_enabled,
        tapo_controls_enabled=payload.tapo_controls_enabled,
        retention_days=payload.retention_days,
        motion_enabled=payload.motion_enabled,
        motion_sensitivity=payload.motion_sensitivity,
        tapo_patrol_mode=payload.tapo_patrol_mode,
        status=CameraStatus.OFFLINE if payload.enabled else CameraStatus.DISABLED,
    )
    if camera.tapo_patrol_mode and (not camera.username or not camera.encrypted_password):
        raise InvalidCameraError(
            "Tapo patrol restoration requires the camera username and stored password."
        )
    session.add(camera)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateCameraNameError(
            f"A camera named '{payload.name.strip()}' already exists."
        ) from exc

    logger.info(
        "Camera added",
        extra={"camera": camera.name, "camera_id": camera.id, "host": camera.host},
    )
    publish(CAMERA_ADDED, {"camera_id": camera.id, "camera": camera.name, "host": camera.host})
    return camera


async def update_camera(session: AsyncSession, camera: Camera, payload: CameraUpdate) -> Camera:
    data = payload.model_dump(exclude_unset=True)
    previous_host = camera.host

    if camera.kind is CameraKind.TAPO_BATTERY:
        # A camera added with the dev build stays in the database untouched,
        # so switching back to that build finds it as it was.
        raise InvalidCameraError(BATTERY_UNSUPPORTED)
    if "host" in data or "tapo_child_id" in data:
        raise InvalidCameraError(
            "An RTSP camera's host comes from its stream URL and cannot be set separately."
        )

    if "host" in data:
        camera.host = data.pop("host")

    if data.get("main_stream_url"):
        clean, host, embedded_user, embedded_password = _split_credentials(
            data["main_stream_url"], data.get("username"), data.get("password")
        )
        camera.main_stream_url = clean
        camera.host = host
        if embedded_user:
            data.setdefault("username", embedded_user)
        if embedded_password:
            data["password"] = embedded_password
    data.pop("main_stream_url", None)

    if "onvif_device_xaddr" in data:
        camera.onvif_device_xaddr = _validate_onvif_host(
            data.pop("onvif_device_xaddr"), camera.host
        )
        camera.onvif_ptz_xaddr = None
        camera.ptz_supported = None
        camera.ptz_zoom_supported = False
        camera.ptz_status_message = None
        camera.ptz_last_checked_at = None

    if "onvif_profile_token" in data:
        camera.onvif_profile_token = data.pop("onvif_profile_token") or None
        camera.onvif_ptz_xaddr = None
        camera.ptz_supported = None
        camera.ptz_zoom_supported = False
        camera.ptz_status_message = None
        camera.ptz_last_checked_at = None

    if camera.host != previous_host and "onvif_device_xaddr" not in payload.model_fields_set:
        # Metadata discovered for the old device must never be reused against a
        # replacement camera merely because this row kept the same name.
        camera.onvif_device_xaddr = None
        camera.onvif_ptz_xaddr = None
        camera.onvif_profile_token = None
        camera.ptz_supported = None
        camera.ptz_zoom_supported = False
        camera.ptz_status_message = None
        camera.ptz_last_checked_at = None

    if "sub_stream_url" in data:
        raw = data.pop("sub_stream_url")
        if raw:
            clean, _, _, _ = _split_credentials(raw, None, None)
            camera.sub_stream_url = clean
        else:
            camera.sub_stream_url = None

    if "password" in data:
        password = data.pop("password")
        # An explicit empty string clears the stored credential; omitting the
        # field entirely leaves it untouched.
        camera.encrypted_password = encrypt_secret(password) if password else None

    if "tapo_password" in data:
        tapo_password = data.pop("tapo_password")
        # This credential is independent from RTSP/ONVIF. Newer Tapo firmware
        # may require ``admin`` plus the TP-Link cloud password for local
        # control while the video stream keeps its Camera Account password.
        camera.encrypted_tapo_password = encrypt_secret(tapo_password) if tapo_password else None

    if "enabled" in data:
        enabled = data["enabled"]
        if not enabled:
            camera.status = CameraStatus.DISABLED
        elif camera.status is CameraStatus.DISABLED:
            camera.status = CameraStatus.OFFLINE

    for field, value in data.items():
        setattr(camera, field, value)

    if camera.tapo_patrol_mode and (not camera.username or not camera.encrypted_password):
        raise InvalidCameraError(
            "Tapo patrol restoration requires the camera username and stored password."
        )
    if camera.kind is CameraKind.RTSP and camera.tapo_controls_enabled is True:
        classification = classify_tapo_controls(camera)
        if classification.source is TapoControlSource.OTHER_VENDOR:
            raise InvalidCameraError(
                "Tapo LAN controls cannot be enabled because this camera is identified as "
                f"another vendor. {classification.reason}"
            )

    camera.updated_at = utcnow()
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateCameraNameError("A camera with that name already exists.") from exc

    logger.info("Camera updated", extra={"camera": camera.name, "camera_id": camera.id})
    return camera


async def delete_camera(session: AsyncSession, camera: Camera) -> None:
    name, camera_id = camera.name, camera.id
    await session.delete(camera)
    await session.flush()
    logger.info("Camera removed", extra={"camera": name, "camera_id": camera_id})
    publish(CAMERA_REMOVED, {"camera_id": camera_id, "camera": name})


# --- Testing ------------------------------------------------------------


def _to_response(result: ProbeResult) -> StreamTestResponse:
    return StreamTestResponse(
        ok=True,
        codec=result.codec,
        resolution=result.resolution,
        width=result.width,
        height=result.height,
        fps=result.fps,
        audio_codec=result.audio_codec,
        bitrate_kbps=result.bitrate_kbps,
        can_stream_copy=result.can_stream_copy,
    )


async def test_stream(
    url: str, username: str | None = None, password: str | None = None
) -> StreamTestResponse:
    """Probe a URL that has not been saved yet."""
    try:
        parsed = parse_stream_url(url)
    except InvalidStreamUrlError as exc:
        return StreamTestResponse(ok=False, error=str(exc))

    full_url = build_stream_url(
        parsed.sanitised_url,
        username or parsed.username,
        password or parsed.password,
    )
    try:
        return _to_response(await probe_stream(full_url))
    except ProbeError as exc:
        return StreamTestResponse(ok=False, error=str(exc))


async def test_camera(session: AsyncSession, camera: Camera) -> CameraTestResponse:
    """Probe a saved camera and record what was found.

    A successful probe is also the camera's liveness signal, so status and
    ``last_seen_at`` are updated here rather than in a separate poll.
    """
    main_url = resolve_stream_url(camera)
    if main_url is None:
        return CameraTestResponse(
            main=StreamTestResponse(ok=False, error="This camera has no main stream URL.")
        )

    # Captured before either branch below overwrites it, so a probe that
    # merely confirms the camera is still online (or still down) does not
    # publish a redundant event -- only an actual transition does.
    previous_status = camera.status

    main_result: StreamTestResponse
    try:
        probe = await probe_stream(main_url)
        main_result = _to_response(probe)
        camera.codec = probe.codec
        camera.width = probe.width
        camera.height = probe.height
        camera.fps = probe.fps
        camera.audio_codec = probe.audio_codec
        camera.bitrate_kbps = probe.bitrate_kbps
        camera.status = CameraStatus.ONLINE if camera.enabled else CameraStatus.DISABLED
        camera.status_message = None
        camera.last_seen_at = utcnow()
        if camera.status is CameraStatus.ONLINE and previous_status is not CameraStatus.ONLINE:
            publish(CAMERA_ONLINE, {"camera_id": camera.id, "camera": camera.name})
    except ProbeError as exc:
        main_result = StreamTestResponse(ok=False, error=str(exc))
        if camera.enabled:
            camera.status = CameraStatus.ERROR
        camera.status_message = str(exc)
        if previous_status is CameraStatus.ONLINE and camera.status is not CameraStatus.ONLINE:
            publish(
                CAMERA_OFFLINE,
                {"camera_id": camera.id, "camera": camera.name, "reason": camera.status_message},
            )

    sub_result: StreamTestResponse | None = None
    sub_url = resolve_stream_url(camera, sub=True)
    if sub_url:
        try:
            sub_result = _to_response(await probe_stream(sub_url))
        except ProbeError as exc:
            # A broken substream is not fatal: motion detection falls back to a
            # downscale of the main stream.
            sub_result = StreamTestResponse(ok=False, error=str(exc))

    await session.flush()
    return CameraTestResponse(main=main_result, sub=sub_result)
