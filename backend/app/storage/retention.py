"""Automatic deletion of old footage.

Recording segments are one half of what a sweep reclaims. The other is the
motion evidence: one full-resolution JPEG and one thumbnail per motion event.
Those were never pruned, and on a test installation they became the
larger problem of the two -- 12 GiB across 21,821 files in five days, 9.7 GiB of
it from a single high-resolution camera, on a small system disk that also holds
the database.
Because the sweep only ever counted indexed segments, none of it was visible to
the accounting that decides whether there is room, so the disk filled while
retention reported itself satisfied.

Motion events are now pruned on the same policy and in the same pass. They are
deleted oldest-first like segments, and their files go before their rows for the
same reason.

Three independent limits, any of which can trigger a sweep:

* **Age.** Anything older than the retention period, globally or per camera.
  Applies to segments and to motion evidence alike.
* **Free space.** Keep at least N bytes free, so the disk never reaches the
  point where FFmpeg fails mid-write.
* **Total size.** Never occupy more than N bytes, for a shared volume.

Deletion is always oldest-first and never touches a protected segment. The file
is removed before its row: if the process dies in between, the row survives
pointing at a missing file, the indexer marks it missing, and the next sweep
tidies it up. The reverse order would leave an orphan file that nothing owns and
retention would never reclaim.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.config import Settings, get_settings
from app.database.models import Camera, MotionEvent, RecordingSegment, SegmentStatus
from app.events.bus import STORAGE_WARNING, publish
from app.logging_config import get_logger
from app.recording import paths
from app.security.paths import UnsafePathError, resolve_within
from app.storage.root import StorageStatus, check_storage
from app.system import settings_store

logger = get_logger("STORAGE")

#: Never delete footage newer than this, whatever the limits say. A
#: misconfigured retention of zero days must not wipe out the last few minutes
#: while they are still being recorded.
MINIMUM_AGE_SECONDS = 120

#: Cap on one sweep, so a badly wrong setting cannot delete everything in a
#: single pass with no chance to notice and stop it.
MAX_DELETIONS_PER_SWEEP = 5000

#: A motion event is two small files, so far more of them fit in one pass than
#: segments do. Still bounded, for the same reason.
MAX_EVENT_DELETIONS_PER_SWEEP = 20000

#: Motion JPEGs live on Sentinel's small data/root volume, not on the recordings
#: volume.  On a test installation they grow by roughly 205 MiB/hour, so
#: keeping them for the normal fourteen-day event-retention window would fill a
#: 16 GiB root disk in a few days.  The event metadata remains useful in the
#: timeline for the normal policy; only its heavyweight snapshot and thumbnail
#: are shed after this operational window.
MOTION_EVIDENCE_RETENTION_HOURS = 24


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    retention_days: int | None
    max_bytes: int | None
    min_free_bytes: int | None

    @classmethod
    async def load(cls, session: AsyncSession) -> RetentionPolicy:
        values = await settings_store.get_many(
            session,
            [
                settings_store.STORAGE_RETENTION_DAYS,
                settings_store.STORAGE_MAX_BYTES,
                settings_store.STORAGE_MIN_FREE_BYTES,
            ],
        )
        return cls(
            retention_days=values[settings_store.STORAGE_RETENTION_DAYS],
            max_bytes=values[settings_store.STORAGE_MAX_BYTES],
            min_free_bytes=values[settings_store.STORAGE_MIN_FREE_BYTES],
        )


@dataclass(frozen=True, slots=True)
class SweepResult:
    deleted: int = 0
    freed_bytes: int = 0
    skipped_protected: int = 0
    reason: str | None = None
    events_deleted: int = 0
    """Motion events removed, counted separately from segments.

    Kept apart so "retention deleted 40 things" cannot hide the fact that all
    forty were thumbnails and no footage was actually reclaimed.
    """
    event_bytes_freed: int = 0
    evidence_references_cleared: int = 0
    """Snapshot/thumbnail references cleared after their files were absent or removed.

    This is deliberately distinct from ``events_deleted``: evidence is pruned
    after 24 hours while the motion-event metadata remains until the normal
    global/per-camera retention policy expires.
    """
    evidence_failures: int = 0
    """Managed evidence paths retained because they could not be resolved or removed."""

    @property
    def did_anything(self) -> bool:
        return self.deleted > 0 or self.events_deleted > 0 or self.evidence_references_cleared > 0


@dataclass(frozen=True, slots=True)
class _EvidenceDeletion:
    """Outcome of touching the two managed files belonging to one event."""

    freed_bytes: int = 0
    references_cleared: int = 0
    failures: int = 0


async def total_used_bytes(session: AsyncSession) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(RecordingSegment.size_bytes), 0)).where(
            RecordingSegment.status != SegmentStatus.MISSING
        )
    )
    return int(total or 0)


async def _camera_retention(session: AsyncSession) -> dict[str, int | None]:
    """Per-camera overrides of the global retention period."""
    rows = await session.execute(select(Camera.id, Camera.retention_days))
    return dict(rows.all())  # type: ignore[arg-type]


async def _delete_segment(
    session: AsyncSession, segment: RecordingSegment, root: Path
) -> int | None:
    """Remove one segment's file, then its row.

    The reclaimed byte count can legitimately be zero, so ``None`` is the
    failure signal.  Callers must only count a deletion after the row was
    actually removed.
    """
    # A batch can wait behind earlier filesystem work. Honour protection or an
    # active-state change made after selection before touching this file.
    await session.refresh(segment)
    if (
        segment.protected
        or segment.status not in (SegmentStatus.COMPLETE, SegmentStatus.DAMAGED)
        or segment.ended_at is None
        or segment.ended_at >= datetime.now(UTC) - timedelta(seconds=MINIMUM_AGE_SECONDS)
    ):
        return None
    try:
        absolute = resolve_within(paths.absolute_path(root, segment.path), root)
    except UnsafePathError:
        logger.warning("Refusing unsafe recording path", extra={"segment_id": segment.id})
        return None

    def remove() -> int | None:
        try:
            size = absolute.stat().st_size
            absolute.unlink()
        except FileNotFoundError:
            # Already gone; the row still needs clearing.
            return 0
        except OSError:
            return None
        return size

    size = await asyncio.to_thread(remove)
    if size is None:
        logger.warning("Could not delete a recording file", extra={"segment_id": segment.id})
        return None

    await session.delete(segment)
    return size


async def _delete_motion_evidence(settings: Settings, event: MotionEvent) -> _EvidenceDeletion:
    """Remove one event's snapshot and thumbnail, clearing only proven-gone paths.

    Both paths are stored relative to the data directory, and both are optional:
    an event whose files were already cleared has nothing to do here.  A missing
    file is success because the stale reference can be cleared.  A path that is
    unsafe, unreadable or cannot be unlinked is failure: both the row and that
    reference stay intact so a later sweep can retry instead of orphaning a file
    nothing can find again.
    """
    freed = 0
    cleared = 0
    failures = 0
    for attribute, relative, base in (
        ("snapshot_path", event.snapshot_path, settings.snapshots_dir),
        ("thumbnail_path", event.thumbnail_path, settings.thumbnails_dir),
    ):
        if not relative:
            continue
        # Resolved under its own base directory, so a stored path can never
        # reach outside the tree it belongs to.
        try:
            absolute = resolve_within(relative, base)
        except UnsafePathError:
            logger.warning(
                "Refusing to delete a motion image outside its own directory",
                extra={"event_id": event.id},
            )
            failures += 1
            continue

        def remove(path: Path = absolute) -> tuple[bool, int]:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return True, 0
            except OSError:
                return False, 0
            try:
                path.unlink()
            except FileNotFoundError:
                return True, 0
            except OSError:
                return False, 0
            return True, size

        removed, size = await asyncio.to_thread(remove)
        if not removed:
            logger.warning(
                "Could not delete a motion image; keeping its event reference for retry",
                extra={"event_id": event.id, "evidence": attribute},
            )
            failures += 1
            continue
        freed += size
        setattr(event, attribute, None)
        cleared += 1
    return _EvidenceDeletion(
        freed_bytes=freed,
        references_cleared=cleared,
        failures=failures,
    )


async def _prune_motion_evidence(
    session: AsyncSession,
    settings: Settings,
    *,
    cutoff: datetime,
    exclude_ids: set[str] | None = None,
) -> _EvidenceDeletion:
    """Shed old JPEG evidence while preserving the event's timeline metadata."""
    query = select(MotionEvent).where(
        MotionEvent.protected.is_(False),
        MotionEvent.started_at < cutoff,
        MotionEvent.ended_at.is_not(None),
        MotionEvent.ended_at < cutoff,
        or_(MotionEvent.snapshot_path.is_not(None), MotionEvent.thumbnail_path.is_not(None)),
    )
    if exclude_ids:
        query = query.where(MotionEvent.id.not_in(exclude_ids))
    rows = await session.execute(
        query.order_by(MotionEvent.started_at.asc()).limit(MAX_EVENT_DELETIONS_PER_SWEEP)
    )
    freed = 0
    cleared = 0
    failures = 0
    for event in rows.scalars():
        outcome = await _delete_motion_evidence(settings, event)
        freed += outcome.freed_bytes
        cleared += outcome.references_cleared
        failures += outcome.failures
    return _EvidenceDeletion(
        freed_bytes=freed,
        references_cleared=cleared,
        failures=failures,
    )


async def _prune_motion_events(
    session: AsyncSession,
    settings: Settings,
    *,
    moment: datetime,
    policy: RetentionPolicy,
    overrides: dict[str, int | None],
    safety_cutoff: datetime,
) -> tuple[int, _EvidenceDeletion, set[str]]:
    """Delete motion events past their camera's retention window.

    Protected events are kept: an operator marking one is the same statement as
    protecting a clip. The per-camera override applies here too, so a camera
    configured to keep a month of footage keeps a month of evidence with it.
    """
    if not policy.retention_days:
        return 0, _EvidenceDeletion(), set()

    default_cutoff = moment - timedelta(days=policy.retention_days)
    rows = await session.execute(
        select(MotionEvent)
        .where(
            MotionEvent.protected.is_(False),
            MotionEvent.started_at < safety_cutoff,
            MotionEvent.ended_at.is_not(None),
            MotionEvent.ended_at < safety_cutoff,
        )
        .order_by(MotionEvent.started_at.asc())
        .limit(MAX_EVENT_DELETIONS_PER_SWEEP)
    )
    deleted = 0
    freed = 0
    cleared = 0
    failures = 0
    failed_event_ids: set[str] = set()
    for event in rows.scalars():
        days = overrides.get(event.camera_id) or policy.retention_days
        cutoff = moment - timedelta(days=days) if days else default_cutoff
        if event.started_at >= cutoff:
            continue
        outcome = await _delete_motion_evidence(settings, event)
        freed += outcome.freed_bytes
        cleared += outcome.references_cleared
        failures += outcome.failures
        if outcome.failures or event.snapshot_path or event.thumbnail_path:
            # At least one managed path is still unresolved.  Keeping the row
            # is what lets the next sweep find and retry it.
            failed_event_ids.add(event.id)
            continue
        await session.delete(event)
        deleted += 1
    return (
        deleted,
        _EvidenceDeletion(
            freed_bytes=freed,
            references_cleared=cleared,
            failures=failures,
        ),
        failed_event_ids,
    )


def candidate_filters(cutoff: datetime) -> tuple[ColumnElement[bool], ...]:
    """Shared by preview and sweep: active/incomplete footage is never eligible."""
    return (
        RecordingSegment.protected.is_(False),
        RecordingSegment.status.in_([SegmentStatus.COMPLETE, SegmentStatus.DAMAGED]),
        RecordingSegment.started_at < cutoff,
        RecordingSegment.ended_at.is_not(None),
        RecordingSegment.ended_at < cutoff,
    )


async def _candidates(
    session: AsyncSession,
    *,
    limit: int,
    cutoff: datetime,
    exclude_ids: set[str] | None = None,
    age_filter: ColumnElement[bool] | None = None,
) -> list[RecordingSegment]:
    """Oldest first, never protected, never too recent to be safe."""
    query = select(RecordingSegment).where(*candidate_filters(cutoff))
    if exclude_ids:
        query = query.where(RecordingSegment.id.not_in(exclude_ids))
    if age_filter is not None:
        query = query.where(age_filter)
    rows = await session.execute(query.order_by(RecordingSegment.started_at.asc()).limit(limit))
    return list(rows.scalars())


async def sweep(
    session: AsyncSession,
    root: Path,
    *,
    policy: RetentionPolicy | None = None,
    storage: StorageStatus | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> SweepResult:
    """Apply the retention policy once."""
    moment = now or datetime.now(UTC)
    policy = policy if policy is not None else await RetentionPolicy.load(session)
    runtime_settings = settings or get_settings()
    if storage is None:
        storage = await asyncio.to_thread(check_storage, root)

    if not storage.available:
        # Segment rows cannot be trusted while their volume is unavailable.
        # Motion JPEGs live on the independent data/root volume, however, and
        # its 24-hour ceiling must keep working or a recordings outage can fill
        # the system disk as a second, unrelated failure.
        evidence = await _prune_motion_evidence(
            session,
            runtime_settings,
            cutoff=moment - timedelta(hours=MOTION_EVIDENCE_RETENTION_HOURS),
        )
        await session.flush()
        return SweepResult(
            reason="Recording storage is unavailable; segment retention skipped.",
            event_bytes_freed=evidence.freed_bytes,
            evidence_references_cleared=evidence.references_cleared,
            evidence_failures=evidence.failures,
        )

    safety_cutoff = moment - timedelta(seconds=MINIMUM_AGE_SECONDS)
    deleted = 0
    freed = 0
    events_deleted = 0
    event_bytes = 0
    evidence_references_cleared = 0
    evidence_failures = 0
    failed_evidence_event_ids: set[str] = set()
    reasons: list[str] = []

    # --- Age ------------------------------------------------------------
    if policy.retention_days:
        overrides = await _camera_retention(session)
        default_cutoff = moment - timedelta(days=policy.retention_days)
        camera_cutoffs = [
            and_(
                RecordingSegment.camera_id == camera_id,
                RecordingSegment.started_at < moment - timedelta(days=days),
            )
            for camera_id, days in overrides.items()
            if days
        ]
        default_age = and_(
            RecordingSegment.camera_id.not_in([key for key, days in overrides.items() if days]),
            RecordingSegment.started_at < default_cutoff,
        )
        # Filter age before limiting: long per-camera overrides must not hide
        # expired footage behind the first bounded batch on every sweep.
        candidates = await _candidates(
            session,
            limit=MAX_DELETIONS_PER_SWEEP,
            cutoff=safety_cutoff,
            age_filter=or_(default_age, *camera_cutoffs),
        )
        for segment in candidates:
            days = overrides.get(segment.camera_id) or policy.retention_days
            cutoff = moment - timedelta(days=days) if days else default_cutoff
            if segment.started_at < cutoff:
                reclaimed = await _delete_segment(session, segment, root)
                if reclaimed is not None:
                    freed += reclaimed
                    deleted += 1
        if deleted:
            reasons.append(f"older than {policy.retention_days} days")

        # Motion evidence, on the same policy and in the same pass. Two small
        # files per event, but there are far more events than segments, and
        # nothing else reclaims them.
        events_deleted, final_evidence, failed_evidence_event_ids = await _prune_motion_events(
            session,
            runtime_settings,
            moment=moment,
            policy=policy,
            overrides=overrides,
            safety_cutoff=safety_cutoff,
        )
        event_bytes += final_evidence.freed_bytes
        evidence_references_cleared += final_evidence.references_cleared
        evidence_failures += final_evidence.failures
        if events_deleted and not deleted:
            reasons.append(f"motion evidence older than {policy.retention_days} days")

    # The heavyweight JPEGs have a deliberately shorter lifetime than the
    # events themselves.  This runs even when event retention is disabled: the
    # metadata remains, but the small root disk stays bounded.  Events whose
    # final deletion failed above are excluded so one sweep does not hammer the
    # same unreadable path twice.
    evidence = await _prune_motion_evidence(
        session,
        runtime_settings,
        cutoff=moment - timedelta(hours=MOTION_EVIDENCE_RETENTION_HOURS),
        exclude_ids=failed_evidence_event_ids,
    )
    event_bytes += evidence.freed_bytes
    evidence_references_cleared += evidence.references_cleared
    evidence_failures += evidence.failures
    if evidence.references_cleared:
        reasons.append(f"motion snapshots older than {MOTION_EVIDENCE_RETENTION_HOURS} hours")

    await session.flush()

    # --- Total size -----------------------------------------------------
    if policy.max_bytes:
        # Bound to a local so the predicate closes over an int, not the
        # optional attribute.
        max_bytes = policy.max_bytes
        used = await total_used_bytes(session)
        if used > max_bytes:
            removed, bytes_freed = await _trim_until(
                session,
                root,
                safety_cutoff,
                should_stop=lambda freed_so_far: (used - freed_so_far) <= max_bytes,
            )
            deleted += removed
            freed += bytes_freed
            if removed:
                reasons.append("storage limit reached")
                publish(
                    STORAGE_WARNING,
                    {
                        "deleted": removed,
                        "freed_bytes": bytes_freed,
                        "reason": "storage limit reached",
                    },
                )

    await session.flush()

    # --- Free space -----------------------------------------------------
    if policy.min_free_bytes:
        current = await asyncio.to_thread(check_storage, root)
        if current.available and current.free_bytes < policy.min_free_bytes:
            shortfall = policy.min_free_bytes - current.free_bytes
            removed, bytes_freed = await _trim_until(
                session,
                root,
                safety_cutoff,
                should_stop=lambda freed_so_far: freed_so_far >= shortfall,
            )
            deleted += removed
            freed += bytes_freed
            if removed:
                reasons.append("minimum free space")
                publish(
                    STORAGE_WARNING,
                    {
                        "deleted": removed,
                        "freed_bytes": bytes_freed,
                        "reason": "minimum free space",
                    },
                )

    await session.flush()

    protected = await session.scalar(
        select(func.count())
        .select_from(RecordingSegment)
        .where(RecordingSegment.protected.is_(True))
    )

    if deleted:
        logger.info(
            "Retention sweep removed old recordings",
            extra={"deleted": deleted, "freed_bytes": freed, "reason": ", ".join(reasons)},
        )

    if events_deleted:
        logger.info(
            "Retention sweep removed old motion events",
            extra={"events": events_deleted, "freed_bytes": event_bytes},
        )

    if evidence_references_cleared or evidence_failures:
        logger.info(
            "Retention sweep processed old motion images",
            extra={
                "references_cleared": evidence_references_cleared,
                "freed_bytes": event_bytes,
                "failures": evidence_failures,
            },
        )

    return SweepResult(
        deleted=deleted,
        freed_bytes=freed,
        skipped_protected=int(protected or 0),
        reason=", ".join(reasons) if reasons else None,
        events_deleted=events_deleted,
        event_bytes_freed=event_bytes,
        evidence_references_cleared=evidence_references_cleared,
        evidence_failures=evidence_failures,
    )


async def _trim_until(
    session: AsyncSession,
    root: Path,
    cutoff: datetime,
    *,
    should_stop: Callable[[int], bool],
) -> tuple[int, int]:
    """Delete oldest-first until ``should_stop(freed)`` is true or nothing is left."""
    deleted = 0
    freed = 0
    attempted = 0
    failed_ids: set[str] = set()
    while attempted < MAX_DELETIONS_PER_SWEEP:
        if should_stop(freed):
            break
        batch = await _candidates(
            session,
            limit=min(100, MAX_DELETIONS_PER_SWEEP - attempted),
            cutoff=cutoff,
            exclude_ids=failed_ids,
        )
        if not batch:
            # Everything left is protected or too recent. Stopping here is
            # correct: honouring a size limit must never override protection.
            logger.warning(
                "Retention could not free enough space; remaining recordings are "
                "protected or too recent"
            )
            break
        for segment in batch:
            if should_stop(freed):
                break
            attempted += 1
            reclaimed = await _delete_segment(session, segment, root)
            if reclaimed is None:
                failed_ids.add(segment.id)
                continue
            freed += reclaimed
            deleted += 1
        await session.flush()

    return deleted, freed
