"""Explicit administrator cleanup of indexed recording footage.

Retention is deliberately conservative and bounded.  This module is the
separate, visible escape hatch for an operator who intentionally wants to
remove a camera's history or start the whole recorder with an empty volume.
Saved exports are a different table and directory and are never touched here.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.database.models import RecordingSegment, SegmentStatus
from app.database.session import session_scope
from app.logging_config import get_logger
from app.recording import paths
from app.recording.manager import get_manager as get_recording_manager
from app.security.paths import UnsafePathError, resolve_within

logger = get_logger("STORAGE")

BATCH_SIZE = 250


class CleanupPhase(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CleanupSelection:
    camera_id: str | None = None
    before: datetime | None = None
    include_protected: bool = False

    @property
    def all_history(self) -> bool:
        return self.camera_id is None and self.before is None


@dataclass(frozen=True, slots=True)
class CleanupPreview:
    segment_count: int
    bytes: int
    protected_count: int
    protected_bytes: int


@dataclass(slots=True)
class CleanupSnapshot:
    phase: CleanupPhase = CleanupPhase.IDLE
    job_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    camera_id: str | None = None
    before: datetime | None = None
    include_protected: bool = False
    matched_segments: int = 0
    matched_bytes: int = 0
    deleted_segments: int = 0
    freed_bytes: int = 0
    failed_segments: int = 0
    message: str | None = None
    saved_clips_preserved: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _filters(selection: CleanupSelection) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = [
        RecordingSegment.status.in_([SegmentStatus.COMPLETE, SegmentStatus.DAMAGED]),
        RecordingSegment.ended_at.is_not(None),
        RecordingSegment.ended_at < datetime.now(UTC),
    ]
    if selection.camera_id:
        filters.append(RecordingSegment.camera_id == selection.camera_id)
    if selection.before:
        filters.append(RecordingSegment.started_at < selection.before)
    if not selection.include_protected:
        filters.append(RecordingSegment.protected.is_(False))
    return filters


async def preview(session: AsyncSession, selection: CleanupSelection) -> CleanupPreview:
    """Count the exact indexed selection before any destructive action."""
    row = (
        await session.execute(
            select(
                func.count(RecordingSegment.id),
                func.coalesce(func.sum(RecordingSegment.size_bytes), 0),
                func.coalesce(
                    func.sum(case((RecordingSegment.protected.is_(True), 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (RecordingSegment.protected.is_(True), RecordingSegment.size_bytes),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(*_filters(selection))
        )
    ).one()
    count, total_bytes, protected_count, protected_bytes = row
    return CleanupPreview(
        segment_count=int(count or 0),
        bytes=int(total_bytes or 0),
        protected_count=int(protected_count or 0),
        protected_bytes=int(protected_bytes or 0),
    )


def _remove_batch(root: Path, rows: list[tuple[str, str]]) -> tuple[list[str], int, int]:
    """Unlink one batch without blocking the event loop."""
    removed: list[str] = []
    freed = 0
    failures = 0
    for segment_id, relative in rows:
        try:
            absolute = resolve_within(paths.absolute_path(root, relative), root)
            try:
                size = absolute.stat().st_size
            except FileNotFoundError:
                size = 0
            absolute.unlink(missing_ok=True)
        except (OSError, UnsafePathError):
            failures += 1
            logger.warning(
                "Could not remove a selected recording file",
                extra={"segment_id": segment_id},
            )
            continue
        removed.append(segment_id)
        freed += size
    return removed, freed, failures


class RecordingCleanupManager:
    """Own the single cleanup job allowed to mutate recording storage."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state = CleanupSnapshot()

    def snapshot(self) -> CleanupSnapshot:
        return replace(self._state)

    async def start(
        self,
        selection: CleanupSelection,
        root: Path,
        expected: CleanupPreview,
    ) -> CleanupSnapshot:
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise RuntimeError("A recording cleanup is already running.")
            self._cancel = asyncio.Event()
            now = datetime.now(UTC)
            self._state = CleanupSnapshot(
                phase=CleanupPhase.RUNNING,
                job_id=f"cleanup-{int(now.timestamp() * 1000)}",
                started_at=now,
                camera_id=selection.camera_id,
                before=selection.before,
                include_protected=selection.include_protected,
                matched_segments=expected.segment_count,
                matched_bytes=expected.bytes,
                message="Preparing recording cleanup.",
            )
            self._task = asyncio.create_task(
                self._run(selection, root),
                name="recording-cleanup",
            )
            return self.snapshot()

    async def cancel(self) -> CleanupSnapshot:
        async with self._lock:
            if self._task is None or self._task.done():
                return self.snapshot()
            self._cancel.set()
            self._state.phase = CleanupPhase.CANCELLING
            self._state.message = "Stopping after the current batch."
            return self.snapshot()

    async def stop(self) -> None:
        """Finish the current small batch and stop safely during shutdown."""
        task = self._task
        if task is None or task.done():
            return
        self._cancel.set()
        with contextlib.suppress(Exception):
            await task

    async def _run(self, selection: CleanupSelection, root: Path) -> None:
        manager = get_recording_manager()
        cursor: tuple[datetime, str] | None = None
        try:
            async with manager.storage_maintenance(pause_recorders=selection.all_history):
                self._state.message = (
                    "Recorders paused; removing stored footage."
                    if selection.all_history
                    else "Removing the selected stored footage."
                )
                while not self._cancel.is_set():
                    async with session_scope() as session:
                        query = select(
                            RecordingSegment.id,
                            RecordingSegment.path,
                            RecordingSegment.started_at,
                        ).where(*_filters(selection))
                        if cursor is not None:
                            started_at, segment_id = cursor
                            query = query.where(
                                or_(
                                    RecordingSegment.started_at > started_at,
                                    and_(
                                        RecordingSegment.started_at == started_at,
                                        RecordingSegment.id > segment_id,
                                    ),
                                )
                            )
                        result = await session.execute(
                            query.order_by(
                                RecordingSegment.started_at.asc(),
                                RecordingSegment.id.asc(),
                            ).limit(BATCH_SIZE)
                        )
                        batch = list(result.all())
                        if not batch:
                            break
                        last = batch[-1]
                        cursor = (last.started_at, last.id)
                        removable = [(row.id, row.path) for row in batch]
                        removed, freed, failed = await asyncio.to_thread(
                            _remove_batch, root, removable
                        )
                        if removed:
                            await session.execute(
                                delete(RecordingSegment).where(RecordingSegment.id.in_(removed))
                            )

                    # session_scope commits before progress is announced. The
                    # interface therefore never claims a batch the DB may roll
                    # back even though its files have gone.
                    self._state.deleted_segments += len(removed)
                    self._state.freed_bytes += freed
                    self._state.failed_segments += failed
                    self._state.message = (
                        f"Removed {self._state.deleted_segments:,} of "
                        f"{self._state.matched_segments:,} selected segments."
                    )

            self._state.finished_at = datetime.now(UTC)
            if self._cancel.is_set():
                self._state.phase = CleanupPhase.CANCELLED
                self._state.message = "Cleanup stopped. Already removed footage cannot be restored."
            else:
                self._state.phase = CleanupPhase.COMPLETED
                self._state.message = (
                    "Recording cleanup completed."
                    if self._state.failed_segments == 0
                    else "Cleanup completed, but some files could not be removed."
                )
            logger.info(
                "Administrator recording cleanup finished",
                extra={
                    "deleted": self._state.deleted_segments,
                    "freed_bytes": self._state.freed_bytes,
                    "failed": self._state.failed_segments,
                    "cancelled": self._cancel.is_set(),
                },
            )
        except Exception:
            self._state.phase = CleanupPhase.FAILED
            self._state.finished_at = datetime.now(UTC)
            self._state.message = "Recording cleanup failed. Recording has been resumed."
            logger.exception("Administrator recording cleanup failed")


_manager: RecordingCleanupManager | None = None


def get_manager() -> RecordingCleanupManager:
    global _manager
    if _manager is None:
        _manager = RecordingCleanupManager()
    return _manager


def set_manager(manager: RecordingCleanupManager | None) -> None:
    global _manager
    _manager = manager
