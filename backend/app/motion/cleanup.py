"""Explicit administrator cleanup of motion-event history.

Recording footage and saved exports are deliberately out of scope.  An event
cleanup removes only ``MotionEvent`` rows and their managed snapshot/thumbnail
files.  Motion workers keep running, so a long cleanup cannot create a blind
spot and newly detected events are outside the frozen selection.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import and_, case, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.config import Settings
from app.database.models import MotionEvent, RecordingSegment
from app.database.session import session_scope
from app.logging_config import get_logger
from app.security.paths import UnsafePathError, resolve_within

logger = get_logger("MOTION")

BATCH_SIZE = 250


class CleanupPhase(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EventCleanupSelection:
    camera_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    min_duration_seconds: float | None = None
    max_duration_seconds: float | None = None
    include_protected: bool = False


@dataclass(frozen=True, slots=True)
class EventCleanupPreview:
    event_count: int
    protected_count: int
    evidence_files: int
    evidence_bytes: int
    active_events_excluded: int


@dataclass(slots=True)
class EventCleanupSnapshot:
    phase: CleanupPhase = CleanupPhase.IDLE
    job_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    camera_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    min_duration_seconds: float | None = None
    max_duration_seconds: float | None = None
    include_protected: bool = False
    matched_events: int = 0
    matched_evidence_files: int = 0
    matched_evidence_bytes: int = 0
    deleted_events: int = 0
    deleted_evidence_files: int = 0
    freed_bytes: int = 0
    failed_evidence_files: int = 0
    message: str | None = None
    recordings_preserved: bool = True
    saved_exports_preserved: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _filters(selection: EventCleanupSelection) -> list[ColumnElement[bool]]:
    # An open event belongs to a running worker.  Deleting it would leave that
    # worker holding an ORM object whose row vanished underneath it.
    filters: list[ColumnElement[bool]] = [MotionEvent.ended_at.is_not(None)]
    if selection.camera_id:
        filters.append(MotionEvent.camera_id == selection.camera_id)
    if selection.start:
        filters.append(MotionEvent.started_at >= selection.start)
    if selection.end:
        filters.append(MotionEvent.started_at < selection.end)
    if selection.min_duration_seconds is not None:
        filters.append(MotionEvent.duration_seconds >= selection.min_duration_seconds)
    if selection.max_duration_seconds is not None:
        filters.append(MotionEvent.duration_seconds <= selection.max_duration_seconds)
    if not selection.include_protected:
        filters.append(MotionEvent.protected.is_(False))
    return filters


def _managed_file_stats(root: Path, relative: str | None) -> tuple[int, int]:
    if not relative:
        return 0, 0
    try:
        absolute = resolve_within(root / relative, root)
        return (1, absolute.stat().st_size) if absolute.is_file() else (0, 0)
    except (OSError, UnsafePathError):
        return 0, 0


def _preview_evidence(
    settings: Settings, rows: list[tuple[str | None, str | None]]
) -> tuple[int, int]:
    files = 0
    size = 0
    for snapshot, thumbnail in rows:
        for root, relative in (
            (settings.snapshots_dir, snapshot),
            (settings.thumbnails_dir, thumbnail),
        ):
            found, found_size = _managed_file_stats(root, relative)
            files += found
            size += found_size
    return files, size


async def preview(
    session: AsyncSession,
    selection: EventCleanupSelection,
    settings: Settings,
) -> EventCleanupPreview:
    """Count the frozen selection and its managed evidence before deletion."""
    filters = _filters(selection)
    count_row = (
        await session.execute(
            select(
                func.count(MotionEvent.id),
                func.coalesce(func.sum(case((MotionEvent.protected.is_(True), 1), else_=0)), 0),
            ).where(*filters)
        )
    ).one()
    evidence_rows = [
        (snapshot, thumbnail)
        for snapshot, thumbnail in (
            await session.execute(
                select(MotionEvent.snapshot_path, MotionEvent.thumbnail_path).where(*filters)
            )
        ).all()
    ]
    evidence_files, evidence_bytes = await asyncio.to_thread(
        _preview_evidence, settings, evidence_rows
    )
    active_filters: list[ColumnElement[bool]] = [MotionEvent.ended_at.is_(None)]
    if selection.camera_id:
        active_filters.append(MotionEvent.camera_id == selection.camera_id)
    active = await session.scalar(
        select(func.count()).select_from(MotionEvent).where(*active_filters)
    )
    return EventCleanupPreview(
        event_count=int(count_row[0] or 0),
        protected_count=int(count_row[1] or 0),
        evidence_files=evidence_files,
        evidence_bytes=evidence_bytes,
        active_events_excluded=int(active or 0),
    )


def _remove_evidence(
    settings: Settings,
    rows: list[tuple[str, str | None, str | None]],
) -> tuple[list[str], int, int, int]:
    deleted_ids: list[str] = []
    deleted_files = 0
    freed_bytes = 0
    failures = 0
    for event_id, snapshot, thumbnail in rows:
        for root, relative in (
            (settings.snapshots_dir, snapshot),
            (settings.thumbnails_dir, thumbnail),
        ):
            if not relative:
                continue
            try:
                absolute = resolve_within(root / relative, root)
                try:
                    size = absolute.stat().st_size
                except FileNotFoundError:
                    size = 0
                existed = absolute.is_file()
                absolute.unlink(missing_ok=True)
                if existed:
                    deleted_files += 1
                    freed_bytes += size
            except (OSError, UnsafePathError):
                failures += 1
                logger.warning(
                    "Could not remove selected motion evidence",
                    extra={"event_id": event_id},
                )
        # An unsafe legacy path is never followed, but it should not hold tens
        # of thousands of otherwise deletable metadata rows hostage.
        deleted_ids.append(event_id)
    return deleted_ids, deleted_files, freed_bytes, failures


async def _clear_orphaned_segment_markers(selection: EventCleanupSelection) -> None:
    """Clear derived motion flags only where no surviving event overlaps.

    Footage itself is untouched.  A segment that also overlaps a protected or
    newly-created event keeps its marker.
    """
    remaining = exists(
        select(MotionEvent.id).where(
            MotionEvent.camera_id == RecordingSegment.camera_id,
            MotionEvent.started_at < RecordingSegment.ended_at,
            or_(
                MotionEvent.ended_at.is_(None),
                MotionEvent.ended_at > RecordingSegment.started_at,
            ),
        )
    )
    filters: list[ColumnElement[bool]] = [~remaining]
    if selection.camera_id:
        filters.append(RecordingSegment.camera_id == selection.camera_id)
    if selection.start:
        filters.append(RecordingSegment.ended_at > selection.start)
    if selection.end:
        filters.append(RecordingSegment.started_at < selection.end)
    async with session_scope() as session:
        await session.execute(
            update(RecordingSegment).where(*filters).values(has_motion=False, motion_seconds=0.0)
        )


class EventCleanupManager:
    """Own the single event-history cleanup allowed at one time."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state = EventCleanupSnapshot()

    def snapshot(self) -> EventCleanupSnapshot:
        return replace(self._state)

    async def start(
        self,
        selection: EventCleanupSelection,
        settings: Settings,
        expected: EventCleanupPreview,
    ) -> EventCleanupSnapshot:
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise RuntimeError("An event cleanup is already running.")
            self._cancel = asyncio.Event()
            now = datetime.now(UTC)
            self._state = EventCleanupSnapshot(
                phase=CleanupPhase.RUNNING,
                job_id=f"event-cleanup-{int(now.timestamp() * 1000)}",
                started_at=now,
                camera_id=selection.camera_id,
                start=selection.start,
                end=selection.end,
                min_duration_seconds=selection.min_duration_seconds,
                max_duration_seconds=selection.max_duration_seconds,
                include_protected=selection.include_protected,
                matched_events=expected.event_count,
                matched_evidence_files=expected.evidence_files,
                matched_evidence_bytes=expected.evidence_bytes,
                message="Preparing event cleanup.",
            )
            self._task = asyncio.create_task(self._run(selection, settings), name="event-cleanup")
            return self.snapshot()

    async def cancel(self) -> EventCleanupSnapshot:
        async with self._lock:
            if self._task is None or self._task.done():
                return self.snapshot()
            self._cancel.set()
            self._state.phase = CleanupPhase.CANCELLING
            self._state.message = "Stopping after the current batch."
            return self.snapshot()

    async def stop(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        self._cancel.set()
        with contextlib.suppress(Exception):
            await task

    async def _run(self, selection: EventCleanupSelection, settings: Settings) -> None:
        cursor: tuple[datetime, str] | None = None
        try:
            while not self._cancel.is_set():
                async with session_scope() as session:
                    query = select(
                        MotionEvent.id,
                        MotionEvent.snapshot_path,
                        MotionEvent.thumbnail_path,
                        MotionEvent.started_at,
                    ).where(*_filters(selection))
                    if cursor is not None:
                        started_at, event_id = cursor
                        query = query.where(
                            or_(
                                MotionEvent.started_at > started_at,
                                and_(
                                    MotionEvent.started_at == started_at,
                                    MotionEvent.id > event_id,
                                ),
                            )
                        )
                    result = await session.execute(
                        query.order_by(MotionEvent.started_at.asc(), MotionEvent.id.asc()).limit(
                            BATCH_SIZE
                        )
                    )
                    batch = list(result.all())
                    if not batch:
                        break
                    last = batch[-1]
                    cursor = (last.started_at, last.id)
                    removable = [(row.id, row.snapshot_path, row.thumbnail_path) for row in batch]
                    deleted_ids, deleted_files, freed, failures = await asyncio.to_thread(
                        _remove_evidence, settings, removable
                    )
                    if deleted_ids:
                        await session.execute(
                            delete(MotionEvent).where(MotionEvent.id.in_(deleted_ids))
                        )

                self._state.deleted_events += len(deleted_ids)
                self._state.deleted_evidence_files += deleted_files
                self._state.freed_bytes += freed
                self._state.failed_evidence_files += failures
                self._state.message = (
                    f"Removed {self._state.deleted_events:,} of "
                    f"{self._state.matched_events:,} selected events."
                )

            await _clear_orphaned_segment_markers(selection)
            self._state.finished_at = datetime.now(UTC)
            if self._cancel.is_set():
                self._state.phase = CleanupPhase.CANCELLED
                self._state.message = "Cleanup stopped. Removed events cannot be restored."
            else:
                self._state.phase = CleanupPhase.COMPLETED
                self._state.message = (
                    "Event cleanup completed."
                    if self._state.failed_evidence_files == 0
                    else "Event cleanup completed; unsafe or unavailable image files were skipped."
                )
            logger.info(
                "Administrator event cleanup finished",
                extra={
                    "deleted": self._state.deleted_events,
                    "freed_bytes": self._state.freed_bytes,
                    "evidence_failures": self._state.failed_evidence_files,
                    "cancelled": self._cancel.is_set(),
                },
            )
        except Exception:
            self._state.phase = CleanupPhase.FAILED
            self._state.finished_at = datetime.now(UTC)
            self._state.message = "Event cleanup failed. Motion detection is still running."
            logger.exception("Administrator event cleanup failed")


_manager: EventCleanupManager | None = None


def get_manager() -> EventCleanupManager:
    global _manager
    if _manager is None:
        _manager = EventCleanupManager()
    return _manager


def set_manager(manager: EventCleanupManager | None) -> None:
    global _manager
    _manager = manager
