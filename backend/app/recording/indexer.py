"""Indexing of recorded segments into the database.

The recorder and the index are deliberately decoupled. FFmpeg writes files and
knows nothing about the database; the indexer reads the directory and reconciles
it. That separation is what makes crash recovery free: whatever FFmpeg managed
to write before a power cut is picked up on the next scan, and a database
restored from backup re-discovers everything still on disk.

Every newly seen file is probed. That costs a few milliseconds on a local file
and is what turns "this segment is corrupt" into a recorded fact rather than a
playback failure the user discovers weeks later.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cameras.probe import ProbeError, probe_stream
from app.database.models import RecordingSegment, SegmentStatus
from app.logging_config import get_logger
from app.recording import paths

logger = get_logger("RECORDING")

#: A file younger than this may still be open; the next sweep will catch it.
IN_PROGRESS_GRACE_SECONDS = 5.0

#: Below this a segment is almost certainly a failed start rather than footage.
MIN_USEFUL_BYTES = 1024


@dataclass(frozen=True, slots=True)
class IndexResult:
    indexed: int = 0
    damaged: int = 0
    missing: int = 0
    skipped: int = 0

    def __add__(self, other: IndexResult) -> IndexResult:
        return IndexResult(
            indexed=self.indexed + other.indexed,
            damaged=self.damaged + other.damaged,
            missing=self.missing + other.missing,
            skipped=self.skipped + other.skipped,
        )


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """One file on disk, with everything the indexer needs about it.

    Collected in a worker thread so the event loop is never blocked: a month of
    footage from a handful of cameras is tens of thousands of files, and walking that tree
    synchronously would stall every request for seconds.
    """

    path: Path
    relative: str
    size_bytes: int
    modified_at: datetime
    is_newest: bool


def scan_camera_files(root: Path, camera_id: str) -> list[ScannedFile]:
    """Walk one camera's directory. Blocking; call through ``asyncio.to_thread``."""
    files = paths.iter_segment_files(root, camera_id)
    if not files:
        return []

    newest = files[-1]
    scanned: list[ScannedFile] = []
    for path in files:
        try:
            relative = paths.relative_path(root, path)
        except ValueError:
            # A symlink pointing outside the storage root; never index it.
            logger.warning("Segment outside the storage root; ignored", extra={"file": str(path)})
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        scanned.append(
            ScannedFile(
                path=path,
                relative=relative,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                is_newest=path == newest,
            )
        )
    return scanned


def existing_relatives(root: Path, relatives: list[str]) -> set[str]:
    """Which of ``relatives`` still exist. Blocking; run in a thread."""
    return {relative for relative in relatives if paths.absolute_path(root, relative).exists()}


def _is_probably_open(scanned: ScannedFile, *, now: datetime) -> bool:
    """Whether FFmpeg is likely still writing this file.

    Only the newest file can be open, and only if it was touched recently. A
    newest file that has not changed for a while means recording stopped, so it
    is final and should be indexed.
    """
    if not scanned.is_newest:
        return False
    return (now - scanned.modified_at) < timedelta(seconds=IN_PROGRESS_GRACE_SECONDS)


async def index_camera(
    session: AsyncSession,
    camera_id: str,
    root: Path,
    *,
    now: datetime | None = None,
) -> IndexResult:
    """Index every complete, unseen segment for one camera."""
    moment = now or datetime.now(UTC)
    files = await asyncio.to_thread(scan_camera_files, root, camera_id)
    known = await _known_paths(session, camera_id)

    # Not an early return when `files` is empty: a camera whose footage was all
    # deleted, or whose storage was unmounted, still has rows that must be
    # marked missing. Returning here would leave the timeline claiming
    # recordings that are gone.
    if not files:
        return await _mark_missing(session, camera_id, root)

    result = IndexResult()
    for scanned in files:
        if scanned.relative in known:
            continue
        if _is_probably_open(scanned, now=moment):
            result += IndexResult(skipped=1)
            continue
        result += await _index_file(session, camera_id, scanned)

    result += await _mark_missing(session, camera_id, root)
    return result


async def _known_paths(session: AsyncSession, camera_id: str) -> set[str]:
    rows = await session.execute(
        select(RecordingSegment.path).where(RecordingSegment.camera_id == camera_id)
    )
    return set(rows.scalars())


async def _index_file(session: AsyncSession, camera_id: str, scanned: ScannedFile) -> IndexResult:
    relative = scanned.relative
    started_at = paths.parse_segment_start(relative)
    if started_at is None:
        logger.debug("Unrecognised file in the recordings directory", extra={"file": relative})
        return IndexResult(skipped=1)

    size = scanned.size_bytes
    if size < MIN_USEFUL_BYTES:
        # FFmpeg creates the file before the first keyframe arrives; a segment
        # this small is a failed start, not footage.
        segment = RecordingSegment(
            camera_id=camera_id,
            path=relative,
            started_at=started_at,
            size_bytes=size,
            status=SegmentStatus.DAMAGED,
        )
        session.add(segment)
        # Do not flush here. A flush takes SQLite's single writer lock, and the
        # remainder of this sweep still has other camera files to FFprobe. In
        # production that held the lock for several seconds at a time and made
        # otherwise unrelated motion-event updates fail with ``database is
        # locked``. The surrounding session commits the whole short batch once
        # every probe and filesystem read has finished.
        return IndexResult(damaged=1)

    status = SegmentStatus.COMPLETE
    duration: float | None = None
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None

    try:
        probe = await probe_stream(str(scanned.path), timeout_seconds=15)
        duration = probe.duration_seconds
        codec = probe.codec
        width = probe.width
        height = probe.height
        fps = probe.fps
    except ProbeError as exc:
        # Typically a segment cut short by a power cut. Recorded as damaged so
        # the gap is visible in the timeline instead of silently absent.
        status = SegmentStatus.DAMAGED
        logger.warning(
            "Segment could not be read; marked as damaged",
            extra={"file": relative, "detail": str(exc)},
        )

    segment = RecordingSegment(
        camera_id=camera_id,
        path=relative,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=duration) if duration else None,
        duration_seconds=duration,
        size_bytes=size,
        codec=codec,
        width=width,
        height=height,
        fps=fps,
        status=status,
    )
    session.add(segment)

    return IndexResult(indexed=1) if status is SegmentStatus.COMPLETE else IndexResult(damaged=1)


async def _mark_missing(session: AsyncSession, camera_id: str, root: Path) -> IndexResult:
    """Flag rows whose file has gone.

    Someone deleting footage by hand, or a storage mount disappearing, should
    show as a known gap rather than a playback error.
    """
    rows = await session.execute(
        select(RecordingSegment).where(
            RecordingSegment.camera_id == camera_id,
            RecordingSegment.status != SegmentStatus.MISSING,
        )
    )
    candidates = list(rows.scalars())
    if not candidates:
        return IndexResult()

    present = await asyncio.to_thread(
        existing_relatives, root, [segment.path for segment in candidates]
    )

    missing = 0
    for segment in candidates:
        if segment.path not in present:
            segment.status = SegmentStatus.MISSING
            missing += 1

    if missing:
        logger.warning(
            "Segments are indexed but no longer on disk",
            extra={"camera_id": camera_id, "count": missing},
        )
    return IndexResult(missing=missing)


async def index_all(
    session: AsyncSession, camera_ids: list[str], root: Path, *, now: datetime | None = None
) -> IndexResult:
    total = IndexResult()
    for camera_id in camera_ids:
        total += await index_camera(session, camera_id, root, now=now)
    if total.indexed or total.damaged or total.missing:
        logger.info(
            "Segment index updated",
            extra={
                "indexed": total.indexed,
                "damaged": total.damaged,
                "missing": total.missing,
            },
        )
    return total
