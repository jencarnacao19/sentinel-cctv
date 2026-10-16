"""Persistence for camera incidents and the recording facts behind them.

An incident is the durable half of the Reliability Center. A live status view
answers "is this camera fine now?"; an incident row answers "did it stop at
04:12, for eleven minutes, and did it come back on its own?" -- which is the
question anybody actually asks the morning after.

Three properties are deliberate:

* **One open incident per camera.** A camera cycling through restart, no fresh
  footage and restart again is one outage, not thirty rows. The open incident
  keeps the worst reason seen (see ``app.reliability.state.worst``), so the
  headline stays the most severe thing that happened rather than whichever
  symptom the last tick caught.
* **Nothing sensitive is stored.** ``detail`` is a sentence Sentinel composed
  itself from a fixed vocabulary and is truncated and redacted before it is
  written. Camera output, URLs, hosts and credentials never reach a row.
* **Bounded.** Retention is enforced by age and by count per camera, so a
  camera failing every night for a year cannot grow the database without limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    CameraIncident,
    IncidentReason,
    RecorderSource,
    RecordingSegment,
    SegmentStatus,
    StreamFault,
)
from app.logging_config import get_logger
from app.recording import timeline
from app.reliability.state import worst
from app.security.redaction import redact_text

logger = get_logger("SYSTEM")

#: Matches the column width; a detail is a sentence, never a log.
MAX_DETAIL_CHARS = 240

#: Resolved incidents older than this are removed.
RETENTION_DAYS = 30

#: Hard ceiling per camera, applied to resolved incidents oldest-first. Age
#: alone is not enough: a camera flapping every few minutes for a week could
#: still accumulate thousands of rows inside the retention window.
MAX_PER_CAMERA = 100

#: Newest-first history returned inline with a camera's status.
INLINE_HISTORY = 5

#: Segment lookback for the cheap grouped freshness query. A camera with
#: nothing in this window gets its own indexed lookup instead, so the answer
#: stays exact without scanning a month of rows on every tick.
SEGMENT_LOOKBACK_DAYS = 2


def sanitise_detail(detail: str | None) -> str | None:
    """Bound and redact a detail before it is stored or returned."""
    if not detail:
        return None
    cleaned = redact_text(" ".join(detail.split()))
    if len(cleaned) <= MAX_DETAIL_CHARS:
        return cleaned
    return cleaned[: MAX_DETAIL_CHARS - 1].rstrip() + "…"


async def last_segment_times(
    session: AsyncSession, camera_ids: list[str], *, now: datetime
) -> dict[str, datetime]:
    """Start time of the newest completed segment for each camera.

    Read from the index rather than the recording volume on purpose. The
    database is already the authority for what footage exists, and walking
    directories on every tick would put avoidable I/O on the same disk the
    recorders are writing to.
    """
    if not camera_ids:
        return {}

    cutoff = now - timedelta(days=SEGMENT_LOOKBACK_DAYS)
    rows = await session.execute(
        select(RecordingSegment.camera_id, func.max(RecordingSegment.started_at))
        .where(
            RecordingSegment.camera_id.in_(camera_ids),
            RecordingSegment.status == SegmentStatus.COMPLETE,
            RecordingSegment.started_at >= cutoff,
        )
        .group_by(RecordingSegment.camera_id)
    )
    newest = {camera_id: value for camera_id, value in rows.all() if value is not None}

    # A camera with no recent footage is the interesting case, and the one the
    # bounded query cannot answer. There are only ever a handful of these, and
    # each lookup is a single seek on (camera_id, started_at).
    for camera_id in camera_ids:
        if camera_id in newest:
            continue
        value = await session.scalar(
            select(func.max(RecordingSegment.started_at)).where(
                RecordingSegment.camera_id == camera_id,
                RecordingSegment.status == SegmentStatus.COMPLETE,
            )
        )
        if value is not None:
            newest[camera_id] = value
    return newest


@dataclass(frozen=True, slots=True)
class CoverageWindow:
    """How much of a window one camera actually recorded.

    This is the measurement the previous freshness check could not make. A
    recorder that writes a four-second segment every ninety seconds has a
    perfectly recent newest segment and records less than five per cent of the
    hour; only summing real segment spans can tell the two apart.
    """

    window_seconds: float
    recorded_seconds: float
    segments: int
    damaged_segments: int
    short_segments: int
    gap_count: int
    longest_gap_seconds: float

    @property
    def ratio(self) -> float:
        """Recorded fraction of the window, clamped to 0..1."""
        if self.window_seconds <= 0:
            return 0.0
        return max(0.0, min(self.recorded_seconds / self.window_seconds, 1.0))

    @property
    def percent(self) -> float:
        return self.ratio * 100.0


#: Segments shorter than this are counted separately. A recorder cutting on
#: clock boundaries produces one short segment per start; dozens of them mean
#: it is restarting or starving, which is the Garagem Exterior signature
#: (92 short segments and a 41-second median in a four-hour window).
SHORT_SEGMENT_SECONDS = 30.0

#: Gaps at or below this are ignored. Segment boundaries are not sample-exact,
#: and a fraction of a second between two files is not missing footage.
GAP_TOLERANCE_SECONDS = 2.0


async def segment_coverage(
    session: AsyncSession,
    camera_ids: list[str],
    *,
    now: datetime,
    window_seconds: float,
    window_starts: dict[str, datetime] | None = None,
) -> dict[str, CoverageWindow]:
    """Measure recorded coverage per camera over the trailing window.

    Uses the same span algebra as the timeline (:func:`app.recording.timeline`)
    so the number an operator reads on the Camera health page and the coverage
    they see on the timeline cannot disagree.

    Damaged segments are subtracted rather than counted: the file exists, but
    the footage in it is not usable, and reporting it as covered would be
    exactly the kind of comfortable lie this feature exists to remove.
    """
    if not camera_ids or window_seconds <= 0:
        return {}

    default_start = now - timedelta(seconds=window_seconds)
    starts = {
        camera_id: max(
            default_start,
            min((window_starts or {}).get(camera_id, default_start), now),
        )
        for camera_id in camera_ids
    }
    query_start = min(starts.values(), default=default_start)
    rows = await session.execute(
        select(
            RecordingSegment.camera_id,
            RecordingSegment.started_at,
            RecordingSegment.ended_at,
            RecordingSegment.duration_seconds,
            RecordingSegment.status,
        ).where(
            RecordingSegment.camera_id.in_(camera_ids),
            RecordingSegment.status != SegmentStatus.MISSING,
            RecordingSegment.started_at < now,
            RecordingSegment.started_at >= query_start - timedelta(seconds=MAX_SEGMENT_SECONDS),
        )
    )

    playable: dict[str, list[timeline.TimeSpan]] = {key: [] for key in camera_ids}
    damaged: dict[str, list[timeline.TimeSpan]] = {key: [] for key in camera_ids}
    counts: dict[str, list[int]] = {key: [0, 0, 0] for key in camera_ids}

    for camera_id, started_at, ended_at, duration, status in rows.all():
        if camera_id not in playable:
            continue
        span_end = _segment_end(started_at, ended_at, duration, status)
        span = timeline.clamp_span(timeline.TimeSpan(started_at, span_end), starts[camera_id], now)
        if span is None:
            continue
        totals = counts[camera_id]
        totals[0] += 1
        if status is SegmentStatus.DAMAGED:
            totals[1] += 1
            damaged[camera_id].append(span)
            continue
        length = (span_end - started_at).total_seconds()
        if length < SHORT_SEGMENT_SECONDS:
            totals[2] += 1
        playable[camera_id].append(span)

    result: dict[str, CoverageWindow] = {}
    for camera_id in camera_ids:
        start = starts[camera_id]
        merged = timeline.merge_spans(playable[camera_id], tolerance_seconds=0.0)
        covered = timeline.subtract_spans(merged, damaged[camera_id])
        absent = timeline.complement_spans(covered, start, now)
        real_gaps = [
            gap for gap in absent if (gap.end - gap.start).total_seconds() > GAP_TOLERANCE_SECONDS
        ]
        longest = max(((gap.end - gap.start).total_seconds() for gap in real_gaps), default=0.0)
        segments, damaged_count, short_count = counts[camera_id]
        result[camera_id] = CoverageWindow(
            # A newly-added camera cannot have recorded time before it
            # existed. Measure its available lifetime until it has accumulated
            # a full window; otherwise ten perfect new segments would be
            # reported as 17% coverage of an impossible hour.
            window_seconds=max((now - start).total_seconds(), 0.0),
            recorded_seconds=timeline.total_seconds(covered),
            segments=segments,
            damaged_segments=damaged_count,
            short_segments=short_count,
            gap_count=len(real_gaps),
            longest_gap_seconds=longest,
        )
    return result


#: Upper bound on how far before the window a segment may start and still
#: overlap it. Segments are a minute by default; an hour is a generous ceiling
#: that keeps the query bounded without ever clipping a real overlap.
MAX_SEGMENT_SECONDS = 3600.0


def _segment_end(
    started_at: datetime,
    ended_at: datetime | None,
    duration: float | None,
    status: SegmentStatus,
) -> datetime:
    """The moment a segment stops covering the timeline.

    Mirrors the timeline's own reasoning: a damaged row with no measured
    duration still occupied real time on disk, so it is given the nominal
    segment length rather than zero width -- otherwise a wall of damaged
    segments would read as a wall of clean gaps, which is a different fault.
    """
    if ended_at is not None and ended_at > started_at:
        return ended_at
    if duration and duration > 0:
        return started_at + timedelta(seconds=duration)
    if status is SegmentStatus.DAMAGED:
        return started_at + timedelta(seconds=timeline.DAMAGED_NOMINAL_SECONDS)
    return started_at


async def active_incidents(session: AsyncSession) -> dict[str, CameraIncident]:
    """The open incident for each camera, keyed by camera id."""
    rows = await session.execute(
        select(CameraIncident)
        .where(CameraIncident.resolved_at.is_(None))
        .order_by(CameraIncident.started_at.desc())
    )
    open_incidents: dict[str, CameraIncident] = {}
    for incident in rows.scalars():
        # Newest first, so the first row per camera wins. A duplicate could
        # only exist after an unclean write; keeping the newest and letting the
        # older one be closed by retention is better than failing here.
        open_incidents.setdefault(incident.camera_id, incident)
    return open_incidents


async def history(
    session: AsyncSession, camera_ids: list[str], *, limit: int = INLINE_HISTORY
) -> dict[str, list[CameraIncident]]:
    """The most recent incidents per camera, newest first."""
    result: dict[str, list[CameraIncident]] = {}
    for camera_id in camera_ids:
        rows = await session.execute(
            select(CameraIncident)
            .where(CameraIncident.camera_id == camera_id)
            .order_by(CameraIncident.started_at.desc())
            .limit(limit)
        )
        result[camera_id] = list(rows.scalars())
    return result


async def list_incidents(
    session: AsyncSession,
    *,
    camera_id: str | None = None,
    active_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[CameraIncident], int]:
    filters = []
    if camera_id:
        filters.append(CameraIncident.camera_id == camera_id)
    if active_only:
        filters.append(CameraIncident.resolved_at.is_(None))

    total = await session.scalar(select(func.count()).select_from(CameraIncident).where(*filters))
    rows = await session.execute(
        select(CameraIncident)
        .where(*filters)
        .order_by(CameraIncident.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars()), int(total or 0)


async def open_incident(
    session: AsyncSession,
    camera_id: str,
    *,
    reason: IncidentReason,
    detail: str | None,
    fault: StreamFault | None,
    source: RecorderSource | None,
    started_at: datetime,
    restart_count: int = 0,
    coverage_ratio: float | None = None,
    damaged_segments: int = 0,
) -> CameraIncident:
    incident = CameraIncident(
        camera_id=camera_id,
        reason=reason,
        started_at=started_at,
        detail=sanitise_detail(detail),
        fault=fault,
        source=source,
        restart_count=max(restart_count, 0),
        coverage_ratio=coverage_ratio,
        damaged_segments=max(damaged_segments, 0),
    )
    session.add(incident)
    await session.flush()
    logger.warning(
        "Camera incident opened",
        extra={"camera_id": camera_id, "reason": reason.value, "incident_id": incident.id},
    )
    return incident


def update_incident(
    incident: CameraIncident,
    *,
    reason: IncidentReason,
    detail: str | None,
    fault: StreamFault | None,
    source: RecorderSource | None,
    restart_increment: int,
    coverage_ratio: float | None = None,
    damaged_segments: int = 0,
) -> bool:
    """Fold a fresh observation into an already-open incident.

    The reason only ever moves toward the more severe end. Letting it drift
    both ways would make the code unstable for anything reading it -- the point
    of a stable reason is that "what happened at 04:12" has one answer.
    """
    changed = False
    escalated = worst(reason, incident.reason) or incident.reason
    if escalated is not incident.reason:
        incident.reason = escalated
        incident.detail = sanitise_detail(detail)
        incident.fault = fault
        changed = True
    elif reason is incident.reason and fault is not None and incident.fault is None:
        # Same reason, but the recorder has since said something classifiable.
        incident.fault = fault
        changed = True
    if source is not None and source is not incident.source:
        incident.source = source
        changed = True
    if coverage_ratio is not None and (
        incident.coverage_ratio is None or coverage_ratio < incident.coverage_ratio
    ):
        # The worst continuity seen while this was open, not the latest: an
        # incident should record how bad it got, not how it happened to look on
        # the tick before it recovered.
        incident.coverage_ratio = coverage_ratio
        changed = True
    if damaged_segments > incident.damaged_segments:
        incident.damaged_segments = damaged_segments
        changed = True
    if restart_increment > 0:
        # The process supervisor exposes a counter for its current lifetime.
        # The monitor converts that into observed deltas, including across a
        # process-object rebuild, so an incident stores restarts that actually
        # happened during that outage rather than an unrelated lifetime total.
        incident.restart_count += restart_increment
        changed = True
    return changed


async def close_incident(
    session: AsyncSession, incident: CameraIncident, *, resolved_at: datetime
) -> CameraIncident:
    incident.resolved_at = resolved_at
    incident.duration_seconds = max((resolved_at - incident.started_at).total_seconds(), 0.0)
    await session.flush()
    logger.info(
        "Camera incident recovered",
        extra={
            "camera_id": incident.camera_id,
            "reason": incident.reason.value,
            "incident_id": incident.id,
            "duration_seconds": round(incident.duration_seconds),
        },
    )
    return incident


async def prune(session: AsyncSession, *, now: datetime) -> int:
    """Enforce both retention limits. Returns how many rows were removed.

    Only resolved incidents are ever deleted: an open one is current state, and
    deleting it would strand a camera in ``recovering`` for ever.
    """
    cutoff = now - timedelta(days=RETENTION_DAYS)
    aged = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(CameraIncident).where(
                CameraIncident.resolved_at.is_not(None),
                # Retention is measured from recovery, not from the beginning of
                # the outage. A month-long incident resolved today is recent and
                # must not disappear during the next prune.
                CameraIncident.resolved_at < cutoff,
            )
        ),
    )
    removed = int(aged.rowcount or 0)

    crowded = await session.execute(
        select(CameraIncident.camera_id)
        .group_by(CameraIncident.camera_id)
        .having(func.count(CameraIncident.id) > MAX_PER_CAMERA)
    )
    for camera_id in crowded.scalars():
        keep = await session.execute(
            select(CameraIncident.id)
            .where(CameraIncident.camera_id == camera_id)
            .order_by(CameraIncident.started_at.desc())
            .limit(MAX_PER_CAMERA)
        )
        surplus = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(CameraIncident).where(
                    CameraIncident.camera_id == camera_id,
                    CameraIncident.resolved_at.is_not(None),
                    CameraIncident.id.not_in(list(keep.scalars())),
                )
            ),
        )
        removed += int(surplus.rowcount or 0)

    if removed:
        await session.flush()
        logger.info("Camera incidents pruned", extra={"removed": removed})
    return removed
