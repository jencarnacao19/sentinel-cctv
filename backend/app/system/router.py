"""System information, health, diagnostics and backup endpoints."""

from __future__ import annotations

import asyncio
import json
import platform
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app import __version__
from app.auth.deps import AdminCameraControlPrincipal, AdminPrincipal, CurrentPrincipal
from app.config import Settings, get_settings
from app.database.session import get_session
from app.logging_config import get_logger
from app.system import backup, diagnostics, health, privacy, settings_store

router = APIRouter(prefix="/system", tags=["system"])
logger = get_logger("SYSTEM")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

#: A configuration backup is kilobytes; this only stops an upload from filling
#: the disk before the archive is even inspected.
MAX_UPLOAD_BYTES = backup.MAX_ARCHIVE_BYTES
UPLOAD_CHUNK_BYTES = 1024 * 1024

_STARTED_AT = time.monotonic()

#: The components the documented health payload always reports as booleans, so
#: an external monitor can rely on a stable shape even as internals change.
_HEALTH_KEYS = ("database", "storage", "go2rtc", "recording")


def uptime_seconds() -> float:
    return time.monotonic() - _STARTED_AT


@router.get("", summary="System information")
async def system_info(
    principal: CurrentPrincipal, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    stored = await settings_store.get_many(
        session, [settings_store.SETUP_COMPLETED, settings_store.TIMEZONE]
    )
    return {
        "name": "Sentinel CCTV",
        "version": __version__,
        "uptime_seconds": round(uptime_seconds(), 3),
        "setup_completed": bool(stored[settings_store.SETUP_COMPLETED]),
        "timezone": stored[settings_store.TIMEZONE],
        "python_version": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
        "authenticated_as": {"kind": principal.kind, "name": principal.name},
    }


@router.get(
    "/health",
    summary="Health check (unauthenticated, used by Docker and monitoring)",
)
async def health_check(response: Response) -> dict[str, Any]:
    overall, components = await health.collect()

    payload: dict[str, Any] = {"status": overall}
    for key in _HEALTH_KEYS:
        component = components.get(key)
        payload[key] = component.is_ok if component is not None else False

    payload["components"] = {
        name: {
            "state": component.state.value,
            **({"detail": component.detail} if component.detail else {}),
            **({"data": component.data} if component.data else {}),
        }
        for name, component in components.items()
    }
    payload["version"] = __version__
    payload["uptime_seconds"] = round(uptime_seconds(), 3)

    if overall == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload


@router.get(
    "/privacy",
    response_model=privacy.PrivacyStatus,
    summary="Global camera privacy state",
)
async def privacy_status(_: CurrentPrincipal) -> privacy.PrivacyStatus:
    return privacy.get_manager().status()


@router.put(
    "/privacy",
    response_model=privacy.PrivacyStatus,
    summary="Pause or resume every camera service",
)
async def update_privacy(
    payload: privacy.PrivacyUpdate,
    principal: AdminCameraControlPrincipal,
    session: SessionDep,
) -> privacy.PrivacyStatus:
    try:
        return await privacy.get_manager().update(
            session,
            enabled=payload.enabled,
            reason=payload.reason,
            changed_by=principal.name,
        )
    except privacy.PrivacyTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


# --- Diagnostics --------------------------------------------------------


async def _subsystem_snapshot() -> dict[str, Any]:
    """Per-subsystem detail for the diagnostics page.

    Imported lazily so this module does not pull the whole engine in at import
    time, which would make a circular import out of a reporting concern.
    """
    from app.motion.manager import get_manager as get_motion_manager
    from app.recording.manager import get_manager as get_recording_manager
    from app.streaming.manager import get_manager as get_streaming_manager

    recording = get_recording_manager()
    storage = recording.storage
    return {
        "recording": {
            "suspended_reason": recording.suspended_reason,
            "recorders": recording.recorder_status(),
        },
        "motion": {"workers": get_motion_manager().worker_status()},
        "go2rtc": {
            "ready": get_streaming_manager().is_ready,
            "process": get_streaming_manager().process_status(),
        },
        "storage": {
            "available": bool(storage and storage.available),
            "reason": storage.reason if storage else None,
        },
    }


@router.get("/diagnostics", summary="Detailed system diagnostics")
async def system_diagnostics(_: AdminPrincipal, settings: SettingsDep) -> dict[str, Any]:
    report = await diagnostics.collect(settings, subsystems=await _subsystem_snapshot())
    return diagnostics.to_payload(report)


@router.get(
    "/diagnostics/download",
    summary="Download a diagnostics bundle for troubleshooting",
    response_class=Response,
)
async def download_diagnostics(_: AdminPrincipal, settings: SettingsDep) -> Response:
    """The same report as a file, for pasting into an issue.

    Everything in it has been through redaction: this is exactly the artefact a
    user shares publicly.
    """
    report = await diagnostics.collect(settings, subsystems=await _subsystem_snapshot())
    body = json.dumps(diagnostics.to_payload(report), indent=2, ensure_ascii=False)
    stamp = report.generated_at.strftime("%Y%m%d-%H%M%S")
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="sentinel-diagnostics-{stamp}.json"'
        },
    )


# --- Backup -------------------------------------------------------------


@router.post(
    "/backup",
    summary="Download a configuration backup",
    response_class=FileResponse,
)
async def create_backup(_: AdminPrincipal, settings: SettingsDep) -> FileResponse:
    """Configuration only: cameras, zones, retention, users and tokens.

    Recordings are excluded, and so are camera passwords -- they are encrypted
    with a secret that stays on this machine, so they have to be entered again
    after a restore.
    """
    # Written to a temporary file rather than held in memory: the archive is
    # small, but streaming it from disk keeps the peak flat regardless.
    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)  # noqa: SIM115
    handle.close()
    destination = Path(handle.name)

    try:
        await backup.create_backup(settings, destination)
    except backup.BackupError as exc:
        await _discard(destination)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return FileResponse(
        destination,
        media_type="application/zip",
        filename=backup.suggested_filename(),
        background=BackgroundTask(destination.unlink, missing_ok=True),
    )


@router.post("/backup/inspect", summary="Describe a backup without applying it")
async def inspect_backup(_: AdminPrincipal, file: UploadFile) -> dict[str, Any]:
    path = await _stage_upload(file)
    try:
        manifest = backup.read_manifest(path)
    except backup.BackupError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    finally:
        await _discard(path)
    return manifest.to_dict()


@router.post("/backup/restore", summary="Restore configuration from a backup")
async def restore_backup(
    _: AdminPrincipal, settings: SettingsDep, file: UploadFile
) -> dict[str, Any]:
    """Replaces the configuration database. Sentinel must be restarted after.

    The previous database is kept alongside rather than deleted, so restoring
    the wrong archive is recoverable.
    """
    path = await _stage_upload(file)
    try:
        manifest = await backup.restore_backup(settings, path)
    except backup.BackupError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    finally:
        await _discard(path)

    logger.warning("Configuration restored; a restart is required")
    return {
        "restored": manifest.to_dict(),
        "restart_required": True,
        "message": (
            "Configuration restored. Restart Sentinel to apply it, then re-enter "
            "each camera's password."
        ),
    }


async def _discard(path: Path) -> None:
    """Remove a temporary file off the event loop."""
    await asyncio.to_thread(path.unlink, missing_ok=True)


async def _stage_upload(file: UploadFile) -> Path:
    """Write an upload to a temporary file, refusing anything oversized.

    Read in chunks with a running total rather than trusting the declared
    content length, which a client controls.
    """
    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)  # noqa: SIM115
    total = 0
    try:
        while chunk := await file.read(UPLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="That file is far too large to be a Sentinel backup.",
                )
            handle.write(chunk)
    except HTTPException:
        handle.close()
        await _discard(Path(handle.name))
        raise
    finally:
        if not handle.closed:
            handle.close()
    return Path(handle.name)
