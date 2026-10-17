"""Storage endpoints: usage, policy and a manual retention sweep."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AdminPrincipal, CurrentPrincipal, IntegrationReadPrincipal
from app.config import Settings, get_settings
from app.database.models import RecordingSegment, SegmentStatus
from app.database.session import get_session
from app.logging_config import get_logger
from app.storage import cleanup, policy_preview, retention
from app.storage.metrics import collect_recording_telemetry, estimate_retention_days
from app.storage.root import StorageStatus, check_storage, get_storage_root, prepare_storage
from app.system import settings_store

router = APIRouter(prefix="/storage", tags=["storage"])
logger = get_logger("STORAGE")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class CameraStorageUsage(BaseModel):
    camera_id: str
    camera_name: str
    bytes: int
    segment_count: int
    protected_bytes: int
    oldest_recording: datetime | None
    newest_recording: datetime | None
    recording_rate_bytes_per_hour: float | None


class StorageUsage(BaseModel):
    path: str
    available: bool
    reason: str | None = None
    total_bytes: int
    used_bytes: int
    free_bytes: int
    recordings_bytes: int
    """What Sentinel's own footage occupies, which is not the same as the
    disk's used space when the volume is shared."""
    snapshots_bytes: int
    database_bytes: int
    segment_count: int
    protected_count: int
    protected_bytes: int
    saved_clips_count: int
    saved_clips_bytes: int
    damaged_count: int
    oldest_recording: datetime | None
    newest_recording: datetime | None
    recording_rate_bytes_per_hour: float | None
    rate_window_hours: float | None
    estimated_retention_days: float | None
    estimated_days_remaining: float | None
    configured_retention_days: int | None
    camera_usage: list[CameraStorageUsage]


class StorageSettings(BaseModel):
    path: str | None
    retention_days: int | None
    max_bytes: int | None
    min_free_bytes: int | None


class StorageSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(default=None, max_length=1024)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    max_bytes: int | None = Field(default=None, ge=1024**3)
    min_free_bytes: int | None = Field(default=None, ge=0)
    preview_token: str | None = Field(default=None, max_length=128)
    confirmation: str | None = Field(default=None, max_length=80)


class PolicyPreviewResponse(policy_preview.PolicyImpact):
    proposed_settings: StorageSettings
    preview_token: str
    expires_in_seconds: int = policy_preview.REVIEW_SECONDS
    required_confirmation: str | None


class SweepResponse(BaseModel):
    deleted: int
    freed_bytes: int
    reason: str | None
    events_deleted: int = 0
    """Motion events pruned. Reported separately from footage so a sweep that
    only cleared thumbnails cannot read as one that reclaimed recordings."""
    event_bytes_freed: int = 0
    evidence_references_cleared: int = 0
    """Snapshot/thumbnail references cleared while retaining event metadata."""
    evidence_failures: int = 0
    """Evidence paths kept for a later retry because removal was not safe."""


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_id: str | None = None
    before: datetime | None = None
    include_protected: bool = False
    confirmation: str | None = Field(default=None, max_length=80)

    def selection(self) -> cleanup.CleanupSelection:
        return cleanup.CleanupSelection(
            camera_id=self.camera_id,
            before=self.before,
            include_protected=self.include_protected,
        )


class CleanupPreviewResponse(BaseModel):
    segment_count: int
    bytes: int
    protected_count: int
    protected_bytes: int
    required_confirmation: str
    saved_clips_preserved: bool = True


class CleanupStatusResponse(BaseModel):
    phase: cleanup.CleanupPhase
    job_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    camera_id: str | None
    before: datetime | None
    include_protected: bool
    matched_segments: int
    matched_bytes: int
    deleted_segments: int
    freed_bytes: int
    failed_segments: int
    message: str | None
    saved_clips_preserved: bool


def _confirmation_for(selection: cleanup.CleanupSelection) -> str:
    return "DELETE ALL RECORDINGS" if selection.all_history else "DELETE SELECTED RECORDINGS"


def _directory_size(path: Path) -> int:
    """Blocking; always called through a thread."""
    if not path.is_dir():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


@router.get("", response_model=StorageUsage, summary="Storage usage")
async def storage_usage(
    _: IntegrationReadPrincipal, session: SessionDep, settings: SettingsDep
) -> StorageUsage:
    root = await get_storage_root(session, settings)
    status_info = await asyncio.to_thread(check_storage, root)
    telemetry = await collect_recording_telemetry(session, include_cameras=True)
    policy = await _read_settings(session, settings)
    estimated_retention, estimated_remaining = estimate_retention_days(
        recordings_bytes=telemetry.recordings_bytes,
        free_bytes=status_info.free_bytes,
        min_free_bytes=policy.min_free_bytes,
        recording_rate_bytes_per_hour=telemetry.recording_rate_bytes_per_hour,
        protected_bytes=telemetry.protected_bytes,
        max_bytes=policy.max_bytes,
    )

    snapshots_bytes = await asyncio.to_thread(_directory_size, settings.snapshots_dir)
    thumbnails_bytes = await asyncio.to_thread(_directory_size, settings.thumbnails_dir)
    database_bytes = await asyncio.to_thread(
        lambda: settings.database_path.stat().st_size if settings.database_path.exists() else 0
    )

    return StorageUsage(
        path=str(root),
        available=status_info.available,
        reason=status_info.reason,
        total_bytes=status_info.total_bytes,
        used_bytes=status_info.used_bytes,
        free_bytes=status_info.free_bytes,
        recordings_bytes=telemetry.recordings_bytes,
        snapshots_bytes=snapshots_bytes + thumbnails_bytes,
        database_bytes=database_bytes,
        segment_count=telemetry.segment_count,
        protected_count=telemetry.protected_count,
        protected_bytes=telemetry.protected_bytes,
        saved_clips_count=telemetry.saved_clips_count,
        saved_clips_bytes=telemetry.saved_clips_bytes,
        damaged_count=telemetry.damaged_count,
        oldest_recording=telemetry.oldest_recording,
        newest_recording=telemetry.newest_recording,
        recording_rate_bytes_per_hour=telemetry.recording_rate_bytes_per_hour,
        rate_window_hours=telemetry.rate_window_hours,
        estimated_retention_days=estimated_retention,
        estimated_days_remaining=estimated_remaining,
        configured_retention_days=policy.retention_days,
        camera_usage=[
            CameraStorageUsage(
                camera_id=camera.camera_id,
                camera_name=camera.camera_name,
                bytes=camera.bytes,
                segment_count=camera.segment_count,
                protected_bytes=camera.protected_bytes,
                oldest_recording=camera.oldest_recording,
                newest_recording=camera.newest_recording,
                recording_rate_bytes_per_hour=camera.recording_rate_bytes_per_hour,
            )
            for camera in telemetry.cameras
        ],
    )


async def _read_settings(session: AsyncSession, settings: Settings) -> StorageSettings:
    values = await settings_store.get_many(
        session,
        [
            settings_store.STORAGE_PATH,
            settings_store.STORAGE_RETENTION_DAYS,
            settings_store.STORAGE_MAX_BYTES,
            settings_store.STORAGE_MIN_FREE_BYTES,
        ],
    )
    return StorageSettings(
        path=values[settings_store.STORAGE_PATH] or str(settings.recordings_dir),
        retention_days=values[settings_store.STORAGE_RETENTION_DAYS],
        max_bytes=values[settings_store.STORAGE_MAX_BYTES],
        min_free_bytes=values[settings_store.STORAGE_MIN_FREE_BYTES],
    )


@router.get("/settings", response_model=StorageSettings, summary="Retention policy")
async def get_storage_settings(
    _: IntegrationReadPrincipal, session: SessionDep, settings: SettingsDep
) -> StorageSettings:
    return await _read_settings(session, settings)


@router.patch("/settings", response_model=StorageSettings, summary="Update retention policy")
async def update_storage_settings(
    payload: StorageSettingsUpdate,
    _: AdminPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> StorageSettings:
    data = payload.model_dump(exclude_unset=True, exclude={"preview_token", "confirmation"})
    current = (await _read_settings(session, settings)).model_dump()
    proposed = {**current, **data}
    if policy_preview.needs_review(current, proposed):
        _impact, fingerprint = await _policy_impact(session, settings, current, proposed)
        if payload.confirmation != policy_preview.CONFIRMATION or not policy_preview.consume_review(
            payload.preview_token, fingerprint
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Preview this policy and type APPLY RETENTION POLICY before saving. "
                "The review expires after five minutes or when the affected footage changes.",
            )

    if data.get("path"):
        # Validated before it is stored: pointing recording at an unusable path
        # would suspend recording at the next tick with no explanation here.
        # expanduser() reads the environment, so it belongs in the thread too.
        raw_path = str(data["path"])

        def prepare() -> tuple[Path, StorageStatus]:
            resolved = Path(raw_path).expanduser()
            return resolved, prepare_storage(resolved)

        candidate, result = await asyncio.to_thread(prepare)
        if not result.available:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=result.reason or "That storage path cannot be used.",
            )
        await settings_store.set_setting(session, settings_store.STORAGE_PATH, str(candidate))
        logger.info("Storage path changed", extra={"path": str(candidate)})

    for key, setting in (
        ("retention_days", settings_store.STORAGE_RETENTION_DAYS),
        ("max_bytes", settings_store.STORAGE_MAX_BYTES),
        ("min_free_bytes", settings_store.STORAGE_MIN_FREE_BYTES),
    ):
        if key in data:
            await settings_store.set_setting(session, setting, data[key])

    await session.commit()
    return await _read_settings(session, settings)


async def _policy_impact(
    session: AsyncSession, settings: Settings, current: dict, proposed: dict
) -> tuple[policy_preview.PolicyImpact, str]:
    if proposed["path"] != current["path"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Change the storage path separately, then preview the policy on that volume.",
        )
    root = await get_storage_root(session, settings)
    storage = await asyncio.to_thread(check_storage, root)
    if not storage.available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=storage.reason or "Storage unavailable; cannot measure policy impact.",
        )
    if (proposed["min_free_bytes"] or 0) >= storage.total_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The free-space reserve must be smaller than the recording volume.",
        )
    return await policy_preview.preview_policy(session, storage, current, proposed)


@router.post(
    "/settings/preview", response_model=PolicyPreviewResponse, summary="Preview retention changes"
)
async def preview_storage_settings(
    payload: StorageSettingsUpdate,
    _: AdminPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> PolicyPreviewResponse:
    current = (await _read_settings(session, settings)).model_dump()
    proposed = {
        **current,
        **payload.model_dump(exclude_unset=True, exclude={"preview_token", "confirmation"}),
    }
    impact, fingerprint = await _policy_impact(session, settings, current, proposed)
    return PolicyPreviewResponse(
        **impact.model_dump(),
        proposed_settings=StorageSettings(**proposed),
        preview_token=policy_preview.remember_review(fingerprint),
        required_confirmation=(
            policy_preview.CONFIRMATION if policy_preview.needs_review(current, proposed) else None
        ),
    )


@router.post(
    "/sweep",
    response_model=SweepResponse,
    summary="Apply the retention policy now",
)
async def run_sweep(_: AdminPrincipal, session: SessionDep, settings: SettingsDep) -> SweepResponse:
    root = await get_storage_root(session, settings)
    result = await retention.sweep(session, root, settings=settings)
    await session.commit()
    return SweepResponse(
        deleted=result.deleted,
        freed_bytes=result.freed_bytes,
        reason=result.reason,
        events_deleted=result.events_deleted,
        event_bytes_freed=result.event_bytes_freed,
        evidence_references_cleared=result.evidence_references_cleared,
        evidence_failures=result.evidence_failures,
    )


@router.post(
    "/cleanup/preview",
    response_model=CleanupPreviewResponse,
    summary="Preview an explicit recording cleanup",
)
async def preview_cleanup(
    payload: CleanupRequest,
    _: AdminPrincipal,
    session: SessionDep,
) -> CleanupPreviewResponse:
    selection = payload.selection()
    result = await cleanup.preview(session, selection)
    return CleanupPreviewResponse(
        segment_count=result.segment_count,
        bytes=result.bytes,
        protected_count=result.protected_count,
        protected_bytes=result.protected_bytes,
        required_confirmation=_confirmation_for(selection),
    )


@router.get(
    "/cleanup",
    response_model=CleanupStatusResponse,
    summary="Recording cleanup progress",
)
async def cleanup_status(_: AdminPrincipal) -> CleanupStatusResponse:
    return CleanupStatusResponse.model_validate(cleanup.get_manager().snapshot().as_dict())


@router.post(
    "/cleanup",
    response_model=CleanupStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an explicit recording cleanup",
)
async def start_cleanup(
    payload: CleanupRequest,
    _: AdminPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> CleanupStatusResponse:
    selection = payload.selection()
    required = _confirmation_for(selection)
    if payload.confirmation != required:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f'Type "{required}" to confirm this irreversible cleanup.',
        )
    root = await get_storage_root(session, settings)
    storage = await asyncio.to_thread(check_storage, root)
    if not storage.available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=storage.reason or "Recording storage is unavailable.",
        )
    expected = await cleanup.preview(session, selection)
    if expected.segment_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No stored recording segments match this selection.",
        )
    try:
        snapshot = await cleanup.get_manager().start(selection, root, expected)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return CleanupStatusResponse.model_validate(snapshot.as_dict())


@router.delete(
    "/cleanup",
    response_model=CleanupStatusResponse,
    summary="Stop a running recording cleanup",
)
async def cancel_cleanup(_: AdminPrincipal) -> CleanupStatusResponse:
    snapshot = await cleanup.get_manager().cancel()
    return CleanupStatusResponse.model_validate(snapshot.as_dict())


@router.get("/timeline-range", summary="Earliest and latest footage per camera")
async def timeline_range(_: CurrentPrincipal, session: SessionDep) -> dict[str, object]:
    """Bounds for the timeline's calendar, so it cannot offer empty days."""
    rows = await session.execute(
        select(
            RecordingSegment.camera_id,
            func.min(RecordingSegment.started_at),
            func.max(RecordingSegment.started_at),
        )
        .where(RecordingSegment.status != SegmentStatus.MISSING)
        .group_by(RecordingSegment.camera_id)
    )
    cameras = {
        camera_id: {"from": oldest, "to": newest} for camera_id, oldest, newest in rows.all()
    }
    now = datetime.now(UTC)
    return {
        "cameras": cameras,
        "now": now,
        "default_window": {"from": now - timedelta(hours=1), "to": now},
    }
