"""What a camera's day looks like, and where to start playing.

Footage is stored as 60-second files, but nobody thinks in segments. This module
answers the questions the interface actually asks:

* *What does this range look like?* -- contiguous runs of footage, the bands
  where movement was detected (with how intense, so the timeline can shade it),
  and the holes where footage should be but is not -- each holding a reason, not
  left for the client to infer from an absence.
* *What plays at 14:32:43?* -- one segment and an offset into it, plus the
  segment that follows, so the player can pre-load and cross the boundary
  without the operator ever learning that a boundary exists.
* *What is between 14:00 and 14:30, in order?* -- the ordered, trimmed parts a
  playback manifest or an export needs, shared by ``playback.py`` and
  ``export.py`` so both answer "which segments, in what order, with what
  offsets" the same way.

Two rules run through all of it.

Adjacent segments are joined only when the gap between them is smaller than the
muxer's own roll-over. FFmpeg closes one file and opens the next, and the second
or two that costs is not missing footage. Anything larger is a real gap and is
reported as one.

A damaged or missing segment is never absorbed into a covered run. Unreadable
footage has to look different from footage that is simply there: silently
drawing coverage over it would be a lie the operator only discovers when they
need the clip.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import ColumnElement, Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.database.models import MotionEvent, RecordingSegment, SegmentStatus
from app.logging_config import get_logger

logger = get_logger("RECORDING")

#: Gap below which two segments are one continuous run. FFmpeg's segment muxer
#: costs a second or so to close one file and open the next; treating that as
#: missing footage would shred every timeline into a dashed line.
SEGMENT_JOIN_TOLERANCE_SECONDS = 2.0

#: Ceiling on a segment row that has no end time. Either FFmpeg is writing it
#: right now, or a power cut left it open forever. Without a ceiling the second
#: case paints coverage from the crash to the present day.
OPEN_SEGMENT_MAX_SECONDS = 300.0

#: Same idea for an event whose end was never written. The aggregator caps a
#: single event at fifteen minutes, so anything open beyond that is a leftover.
OPEN_EVENT_MAX_SECONDS = 15 * 60.0

#: Nominal width for a damaged segment with no measurable duration, when nothing
#: follows it to bound it. Better a visible scar than an invisible one.
DAMAGED_NOMINAL_SECONDS = 60.0

#: Spans returned per track before they are coarsened. A 4K-wide timeline is
#: under 4000 pixels, so beyond this the extra spans cannot be drawn anyway.
DEFAULT_MAX_BUCKETS = 2000

#: A month and a day, so "the last month" is expressible without hunting for an
#: off-by-one. Anything beyond this is a mistake or a scrape, not a timeline.
MAX_TIMELINE_SECONDS = 32 * 24 * 3600.0

#: Rows read for one timeline. A month of recording is ~45,000 segments per
#: camera; reading them all to draw 4000 pixels would be pointless.
MAX_TIMELINE_SEGMENTS = 20_000

#: Export ceiling. Four hours of 4 Mbit footage is already a 7 GB download; a
#: request for a week is a mis-click, and concatenating it would fill the disk.
MAX_EXPORT_SECONDS = 4 * 3600.0

MAX_EXPORT_PARTS = 1000

#: How long a gap may trail behind "now" and still be blamed on a *currently*
#: unavailable storage mount. Sentinel does not keep a history of when storage
#: came and went (see ``storage.root``), only whether it is available right
#: now, so a gap that ended minutes ago cannot honestly be attributed to it --
#: the mount could have been fine again since. Three storage-check ticks
#: (``recording.manager.STORAGE_TICK_SECONDS``) of slack covers the gap between
#: the outage actually clearing and the next check noticing.
STORAGE_GAP_ATTRIBUTION_SECONDS = 90.0

_PLAYABLE = (SegmentStatus.COMPLETE, SegmentStatus.RECORDING)


class TimelineRangeError(ValueError):
    """The requested range is unusable: inverted, or wider than the cap."""


class GapReason(StrEnum):
    """Why a stretch of the timeline has no footage.

    ``DAMAGED`` comes straight from the segment's own status. ``NOT_RECORDING``
    is the honest catch-all for everything else Sentinel cannot distinguish
    after the fact -- the camera was disabled, its mode was not continuous, or
    it simply did not exist yet. ``STORAGE_UNAVAILABLE`` is the one exception:
    while the mount is down right now, the trailing gap can be attributed to
    that specific, currently-visible cause.
    """

    NOT_RECORDING = "not_recording"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    DAMAGED = "damaged"


# --- Spans --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeSpan:
    """A half-open interval of wall-clock time, always UTC-aware."""

    start: datetime
    end: datetime

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


def merge_spans(spans: Iterable[TimeSpan], *, tolerance_seconds: float) -> list[TimeSpan]:
    """Join spans separated by no more than ``tolerance_seconds``."""
    ordered = sorted((span for span in spans if span.end > span.start), key=lambda s: s.start)
    merged: list[TimeSpan] = []
    for span in ordered:
        if merged and (span.start - merged[-1].end).total_seconds() <= tolerance_seconds:
            previous = merged[-1]
            if span.end > previous.end:
                merged[-1] = TimeSpan(previous.start, span.end)
            continue
        merged.append(span)
    return merged


def subtract_spans(spans: Iterable[TimeSpan], holes: Iterable[TimeSpan]) -> list[TimeSpan]:
    """Cut ``holes`` out of ``spans``.

    Quadratic, deliberately: holes are damaged segments, and an install with
    enough of those to make this slow has a far larger problem than a timeline.
    """
    punched = merge_spans(holes, tolerance_seconds=0.0)
    if not punched:
        return [span for span in spans if span.end > span.start]

    result: list[TimeSpan] = []
    for span in spans:
        pieces = [span]
        for hole in punched:
            remaining: list[TimeSpan] = []
            for piece in pieces:
                if hole.end <= piece.start or hole.start >= piece.end:
                    remaining.append(piece)
                    continue
                if piece.start < hole.start:
                    remaining.append(TimeSpan(piece.start, hole.start))
                if hole.end < piece.end:
                    remaining.append(TimeSpan(hole.end, piece.end))
            pieces = remaining
        result.extend(piece for piece in pieces if piece.end > piece.start)
    return result


def clamp_span(span: TimeSpan, start: datetime, end: datetime) -> TimeSpan | None:
    """Trim a span to a window, or drop it when nothing is left."""
    clamped = TimeSpan(max(span.start, start), min(span.end, end))
    return clamped if clamped.end > clamped.start else None


def complement_spans(spans: Iterable[TimeSpan], start: datetime, end: datetime) -> list[TimeSpan]:
    """The parts of ``[start, end)`` that ``spans`` does not cover."""
    gaps: list[TimeSpan] = []
    cursor = start
    for span in merge_spans(spans, tolerance_seconds=0.0):
        if span.start > cursor:
            gaps.append(TimeSpan(cursor, span.start))
        cursor = max(cursor, span.end)
    if cursor < end:
        gaps.append(TimeSpan(cursor, end))
    return gaps


def total_seconds(spans: Iterable[TimeSpan]) -> float:
    return sum(span.duration_seconds for span in spans)


# --- Results ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Gap:
    """A stretch of the timeline with no playable footage, and why."""

    start: datetime
    end: datetime
    reason: GapReason

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass(frozen=True, slots=True)
class MotionBand:
    """A period of movement, coarsened for display but never past its peak.

    ``peak_score`` is the strongest of everything folded into the band, not an
    average: the shading exists to say "something happened here", and
    averaging a brief, intense event together with a quiet neighbour would
    hide the one thing worth noticing.
    """

    start: datetime
    end: datetime
    peak_score: float
    event_count: int


@dataclass(frozen=True, slots=True)
class Timeline:
    camera_id: str
    start: datetime
    end: datetime
    coverage: list[TimeSpan]
    """Runs of playable footage. Never overlaps a gap."""
    gaps: list[Gap]
    motion: list[MotionBand]
    recorded_seconds: float
    motion_seconds: float
    damaged_seconds: float
    gap_seconds: float
    """Total time with no playable footage, for any reason."""
    segment_count: int
    motion_event_count: int
    bucket_seconds: float
    """The smallest gap the returned spans can still express. Larger than the
    join tolerance only when the range held more detail than could be drawn."""
    truncated: bool
    """More segments matched than were read; the tail of the range is missing."""


@dataclass(frozen=True, slots=True)
class PlaybackTarget:
    """Where a wall-clock instant lives on disk."""

    camera_id: str
    at: datetime
    segment_id: str | None
    started_at: datetime | None
    ended_at: datetime | None
    offset_seconds: float
    """Seconds into the file that correspond to ``at``."""
    next_segment_id: str | None
    next_started_at: datetime | None
    continuous: bool
    """The next segment begins within the join tolerance, so playback can roll
    straight into it rather than jumping."""
    gap_seconds: float | None
    """When there is no footage at ``at``, how far ahead the next footage is."""

    @property
    def found(self) -> bool:
        return self.segment_id is not None


@dataclass(frozen=True, slots=True)
class ExportPart:
    """One segment's contribution to a range, as trim offsets into the file.

    Shared shape for both an export (which turns it into concat-demuxer
    ``inpoint``/``outpoint`` directives) and a playback manifest (which turns
    it into "append this file, then seek this far into it").
    """

    segment_id: str
    started_at: datetime
    ended_at: datetime
    trim_start_seconds: float
    """Dropped from the head of the file. Non-zero only for the first part."""
    trim_end_seconds: float
    """Dropped from the tail. Non-zero only for the last part."""
    duration_seconds: float
    size_bytes: int
    """Pro-rata share of the file, so the interface can warn before a download."""


@dataclass(frozen=True, slots=True)
class ExportPlan:
    camera_id: str
    start: datetime
    end: datetime
    parts: list[ExportPart]
    gaps: list[TimeSpan]
    """Stretches with no playable footage. Damaged segments appear here: feeding
    one to a concat demuxer corrupts the whole output."""
    total_seconds: float
    estimated_bytes: int

    @property
    def complete(self) -> bool:
        return not self.gaps


# --- Internal rows ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Segment:
    id: str
    started_at: datetime
    ended_at: datetime
    """Effective end: resolved even when the row has none."""
    status: SegmentStatus
    size_bytes: int

    @property
    def file_seconds(self) -> float:
        return max(0.0, (self.ended_at - self.started_at).total_seconds())


def _as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC.

    Query parameters arrive without a timezone often enough that the alternative
    is a TypeError deep inside a comparison, surfacing as a 500.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def check_range(start: datetime, end: datetime, *, max_seconds: float) -> tuple[datetime, datetime]:
    """Validate and normalise a requested window. Shared by every entry point
    below so "too long" and "backwards" are rejected the same way everywhere."""
    start, end = _as_utc(start), _as_utc(end)
    if end <= start:
        raise TimelineRangeError("The end of the range must be after its start.")
    if (end - start).total_seconds() > max_seconds:
        hours = max_seconds / 3600
        raise TimelineRangeError(
            f"That range is too long; ask for at most {hours:g} hours at once."
        )
    return start, end


def _effective_end(
    started_at: datetime,
    ended_at: datetime | None,
    duration_seconds: float | None,
    *,
    next_start: datetime | None,
    now: datetime,
) -> datetime:
    """Resolve the end of a segment row that may not have one.

    A row with no end is either the file being written right now or one a crash
    left open. The next segment's start is the truth in the second case and
    ``now`` bounds the first; the ceiling covers a row with neither.
    """
    if ended_at is not None:
        return ended_at
    if duration_seconds:
        return started_at + timedelta(seconds=duration_seconds)

    end = started_at + timedelta(seconds=OPEN_SEGMENT_MAX_SECONDS)
    if next_start is not None and started_at < next_start < end:
        end = next_start
    if started_at < now < end:
        end = now
    return end


def _overlap_filter(
    started_column: InstrumentedAttribute[datetime],
    ended_column: InstrumentedAttribute[datetime | None],
    start: datetime,
    open_max_seconds: float,
) -> ColumnElement[bool]:
    """Rows overlapping a window, including one still open.

    The open branch is bounded by ``started_at`` as well as by the null check:
    without it every query drags in every row a crash ever left open, and
    SQLite cannot use the (camera, started_at) index to skip them.
    """
    return or_(
        ended_column > start,
        and_(
            ended_column.is_(None),
            started_column > start - timedelta(seconds=open_max_seconds),
        ),
    )


_SegmentRow = tuple[str, datetime, datetime | None, float | None, SegmentStatus, int]


def _segments_query(camera_id: str, start: datetime, end: datetime) -> Select[_SegmentRow]:
    return (
        select(
            RecordingSegment.id,
            RecordingSegment.started_at,
            RecordingSegment.ended_at,
            RecordingSegment.duration_seconds,
            RecordingSegment.status,
            RecordingSegment.size_bytes,
        )
        .where(
            RecordingSegment.camera_id == camera_id,
            RecordingSegment.started_at < end,
            # A missing file is not footage. It leaves a gap, which is exactly
            # what an operator needs to see.
            RecordingSegment.status != SegmentStatus.MISSING,
            _overlap_filter(
                RecordingSegment.started_at,
                RecordingSegment.ended_at,
                start,
                OPEN_SEGMENT_MAX_SECONDS,
            ),
        )
        .order_by(RecordingSegment.started_at.asc())
    )


async def _load_segments(
    session: AsyncSession,
    camera_id: str,
    start: datetime,
    end: datetime,
    *,
    now: datetime,
    limit: int,
) -> tuple[list[_Segment], bool]:
    """Segments overlapping the window, oldest first, with their real ends.

    Only the six columns the timeline needs are selected: the path and codec
    columns would multiply the row size for nothing, and the path must not leave
    this module in any case -- callers that need it (export, download) look it
    up themselves from the segment id.
    """
    rows = (await session.execute(_segments_query(camera_id, start, end).limit(limit + 1))).all()
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
        logger.warning(
            "Timeline range holds more segments than can be read",
            extra={"camera_id": camera_id, "limit": limit},
        )

    segments: list[_Segment] = []
    for index, row in enumerate(rows):
        next_start = rows[index + 1].started_at if index + 1 < len(rows) else None
        resolved = _effective_end(
            row.started_at,
            row.ended_at,
            row.duration_seconds,
            next_start=next_start,
            now=now,
        )
        if resolved <= row.started_at and row.status is SegmentStatus.DAMAGED:
            # A damaged file often has no readable duration at all. Give it a
            # nominal width rather than a zero-width span nobody can see.
            resolved = row.started_at + timedelta(seconds=DAMAGED_NOMINAL_SECONDS)
        segments.append(
            _Segment(
                id=row.id,
                started_at=row.started_at,
                ended_at=resolved,
                status=row.status,
                size_bytes=row.size_bytes,
            )
        )
    return segments, truncated


async def _load_motion_bands(
    session: AsyncSession, camera_id: str, start: datetime, end: datetime, *, now: datetime
) -> list[MotionBand]:
    """One band per event, clamped to the window and carrying its peak score.

    The band is the movement itself, not the clip: pre- and post-roll exist so a
    clip starts before the action, and painting them here would make every
    two-second event look like a twenty-second one.
    """
    rows = (
        await session.execute(
            select(MotionEvent.started_at, MotionEvent.ended_at, MotionEvent.peak_score)
            .where(
                MotionEvent.camera_id == camera_id,
                MotionEvent.started_at < end,
                _overlap_filter(
                    MotionEvent.started_at, MotionEvent.ended_at, start, OPEN_EVENT_MAX_SECONDS
                ),
            )
            .order_by(MotionEvent.started_at.asc())
        )
    ).all()

    bands: list[MotionBand] = []
    for row in rows:
        ended = row.ended_at
        if ended is None:
            ceiling = row.started_at + timedelta(seconds=OPEN_EVENT_MAX_SECONDS)
            ended = now if row.started_at < now < ceiling else ceiling
        clamped = clamp_span(TimeSpan(row.started_at, ended), start, end)
        if clamped is not None:
            bands.append(MotionBand(clamped.start, clamped.end, row.peak_score, 1))
    return bands


def _merge_motion(bands: Iterable[MotionBand], *, tolerance_seconds: float) -> list[MotionBand]:
    """Join adjacent bands, keeping the highest peak and summing how many
    events contributed. Safe to call again on an already-merged list, which is
    exactly what further coarsening for a wide window needs."""
    ordered = sorted((band for band in bands if band.end > band.start), key=lambda b: b.start)
    merged: list[MotionBand] = []
    for band in ordered:
        if merged and (band.start - merged[-1].end).total_seconds() <= tolerance_seconds:
            previous = merged[-1]
            merged[-1] = MotionBand(
                start=previous.start,
                end=max(previous.end, band.end),
                peak_score=max(previous.peak_score, band.peak_score),
                event_count=previous.event_count + band.event_count,
            )
            continue
        merged.append(band)
    return merged


def _merge_gaps(gaps: Iterable[Gap], *, tolerance_seconds: float) -> list[Gap]:
    """Join adjacent gaps, but only within the same reason.

    Merging a damaged gap into a not-recording one either side of it would
    misreport why a stretch has no footage, which is the one thing a gap
    exists to say.
    """
    by_reason: dict[GapReason, list[TimeSpan]] = {}
    for gap in gaps:
        by_reason.setdefault(gap.reason, []).append(TimeSpan(gap.start, gap.end))

    merged: list[Gap] = []
    for reason, spans in by_reason.items():
        merged.extend(
            Gap(span.start, span.end, reason)
            for span in merge_spans(spans, tolerance_seconds=tolerance_seconds)
        )
    merged.sort(key=lambda gap: gap.start)
    return merged


def _classify_absence(span: TimeSpan, moment: datetime, storage_unavailable: bool) -> GapReason:
    trailing_seconds = (moment - span.end).total_seconds()
    if storage_unavailable and trailing_seconds <= STORAGE_GAP_ATTRIBUTION_SECONDS:
        return GapReason.STORAGE_UNAVAILABLE
    return GapReason.NOT_RECORDING


def _bucket_seconds(start: datetime, end: datetime, max_buckets: int) -> float:
    """The finest gap worth reporting for a window of this width."""
    return max(SEGMENT_JOIN_TOLERANCE_SECONDS, (end - start).total_seconds() / max(1, max_buckets))


# --- Public API ---------------------------------------------------------


async def build_timeline(
    session: AsyncSession,
    camera_id: str,
    start: datetime,
    end: datetime,
    *,
    max_buckets: int = DEFAULT_MAX_BUCKETS,
    now: datetime | None = None,
    storage_unavailable: bool = False,
) -> Timeline:
    """Coverage, gaps and motion across one camera's range.

    ``storage_unavailable`` should reflect the recording manager's *live*
    state. Sentinel keeps no history of when storage came and went, so a gap
    can only be blamed on it while the outage is still ongoing (see
    ``STORAGE_GAP_ATTRIBUTION_SECONDS``); every older unexplained gap is
    reported as "not recording", which is honest even when it is not the most
    specific answer available.
    """
    start, end = check_range(start, end, max_seconds=MAX_TIMELINE_SECONDS)
    moment = now or datetime.now(UTC)

    segments, truncated = await _load_segments(
        session, camera_id, start, end, now=moment, limit=MAX_TIMELINE_SEGMENTS
    )

    playable: list[TimeSpan] = []
    broken: list[TimeSpan] = []
    for segment in segments:
        span = clamp_span(TimeSpan(segment.started_at, segment.ended_at), start, end)
        if span is None:
            continue
        (broken if segment.status is SegmentStatus.DAMAGED else playable).append(span)

    damaged = merge_spans(broken, tolerance_seconds=SEGMENT_JOIN_TOLERANCE_SECONDS)
    coverage = subtract_spans(
        merge_spans(playable, tolerance_seconds=SEGMENT_JOIN_TOLERANCE_SECONDS), damaged
    )
    absence = complement_spans(merge_spans(coverage + damaged, tolerance_seconds=0.0), start, end)

    gaps = sorted(
        [Gap(span.start, span.end, GapReason.DAMAGED) for span in damaged]
        + [
            Gap(span.start, span.end, _classify_absence(span, moment, storage_unavailable))
            for span in absence
        ],
        key=lambda gap: gap.start,
    )

    motion_bands = await _load_motion_bands(session, camera_id, start, end, now=moment)
    motion = _merge_motion(motion_bands, tolerance_seconds=SEGMENT_JOIN_TOLERANCE_SECONDS)

    # Totals come from the accurate spans, before any coarsening: what is drawn
    # may be approximate, but "you have 6h 12m of footage" must not be.
    recorded_seconds = total_seconds(coverage)
    damaged_seconds = total_seconds(damaged)
    gap_seconds = sum(gap.duration_seconds for gap in gaps)
    motion_seconds = total_seconds([TimeSpan(band.start, band.end) for band in motion])

    bucket = SEGMENT_JOIN_TOLERANCE_SECONDS
    coarse = _bucket_seconds(start, end, max_buckets)
    if len(coverage) > max_buckets:
        # Damage is subtracted again afterwards: coarsening must never be the
        # thing that paints over an unreadable stretch.
        coverage = subtract_spans(merge_spans(coverage, tolerance_seconds=coarse), damaged)
        bucket = coarse
    if len(motion) > max_buckets:
        motion = _merge_motion(motion, tolerance_seconds=coarse)
        bucket = coarse
    if len(gaps) > max_buckets:
        gaps = _merge_gaps(gaps, tolerance_seconds=coarse)
        bucket = coarse

    return Timeline(
        camera_id=camera_id,
        start=start,
        end=end,
        coverage=coverage,
        gaps=gaps,
        motion=motion,
        recorded_seconds=recorded_seconds,
        motion_seconds=motion_seconds,
        damaged_seconds=damaged_seconds,
        gap_seconds=gap_seconds,
        segment_count=len(segments),
        motion_event_count=len(motion_bands),
        bucket_seconds=bucket,
        truncated=truncated,
    )


async def resolve_playback(
    session: AsyncSession,
    camera_id: str,
    at: datetime,
    *,
    now: datetime | None = None,
) -> PlaybackTarget:
    """Which file plays at ``at``, how far into it, and what follows.

    Two queries rather than a range scan: the segment containing an instant is
    the newest one starting at or before it, and its successor is the oldest one
    starting after it. Both are single index seeks whatever the archive size.
    """
    at = _as_utc(at)
    moment = now or datetime.now(UTC)

    columns = (
        RecordingSegment.id,
        RecordingSegment.started_at,
        RecordingSegment.ended_at,
        RecordingSegment.duration_seconds,
    )
    base = select(*columns).where(
        RecordingSegment.camera_id == camera_id,
        RecordingSegment.status.in_(_PLAYABLE),
    )

    current = (
        await session.execute(
            base.where(RecordingSegment.started_at <= at)
            .order_by(RecordingSegment.started_at.desc())
            .limit(1)
        )
    ).first()
    following = (
        await session.execute(
            base.where(RecordingSegment.started_at > at)
            .order_by(RecordingSegment.started_at.asc())
            .limit(1)
        )
    ).first()

    next_id = following.id if following is not None else None
    next_started = following.started_at if following is not None else None

    if current is not None:
        ended = _effective_end(
            current.started_at,
            current.ended_at,
            current.duration_seconds,
            next_start=next_started,
            now=moment,
        )
        if ended > at:
            continuous = (
                next_started is not None
                and (next_started - ended).total_seconds() <= SEGMENT_JOIN_TOLERANCE_SECONDS
            )
            return PlaybackTarget(
                camera_id=camera_id,
                at=at,
                segment_id=current.id,
                started_at=current.started_at,
                ended_at=ended,
                offset_seconds=max(0.0, (at - current.started_at).total_seconds()),
                next_segment_id=next_id,
                next_started_at=next_started,
                continuous=continuous,
                gap_seconds=None,
            )

    # Nothing covers this instant. Reporting the next footage turns a dead end
    # into "recording resumes at 14:40", which is what the operator wants next.
    return PlaybackTarget(
        camera_id=camera_id,
        at=at,
        segment_id=None,
        started_at=None,
        ended_at=None,
        offset_seconds=0.0,
        next_segment_id=next_id,
        next_started_at=next_started,
        continuous=False,
        gap_seconds=(next_started - at).total_seconds() if next_started is not None else None,
    )


async def plan_parts(
    session: AsyncSession,
    camera_id: str,
    start: datetime,
    end: datetime,
    *,
    now: datetime | None = None,
    limit: int,
) -> tuple[list[ExportPart], list[TimeSpan], bool]:
    """The ordered, trimmed parts covering a range, and the gaps between them.

    Shared by the export planner and the playback manifest: both need "which
    segments, in what order, with what offsets into each file" -- an export
    additionally derives a byte-share estimate from the same parts, a manifest
    just needs the ordering and the offset to seek to. Damaged segments are
    left out and reported as gaps in both cases: a concat demuxer given an
    unreadable input corrupts everything after it, and playback across one
    would either stall or throw a decode error.
    """
    moment = now or datetime.now(UTC)
    segments, truncated = await _load_segments(
        session, camera_id, start, end, now=moment, limit=limit
    )

    parts: list[ExportPart] = []
    covered: list[TimeSpan] = []
    for segment in segments:
        if segment.status is SegmentStatus.DAMAGED:
            continue
        span = clamp_span(TimeSpan(segment.started_at, segment.ended_at), start, end)
        if span is None:
            continue

        file_seconds = segment.file_seconds
        duration = span.duration_seconds
        # Pro-rata rather than exact: only a full re-mux knows the byte offset of
        # a keyframe, and this number exists to warn about a download size.
        share = (
            int(segment.size_bytes * duration / file_seconds)
            if file_seconds > 0
            else segment.size_bytes
        )
        parts.append(
            ExportPart(
                segment_id=segment.id,
                started_at=span.start,
                ended_at=span.end,
                trim_start_seconds=max(0.0, (span.start - segment.started_at).total_seconds()),
                trim_end_seconds=max(0.0, (segment.ended_at - span.end).total_seconds()),
                duration_seconds=duration,
                size_bytes=share,
            )
        )
        covered.append(span)

    gaps = complement_spans(
        merge_spans(covered, tolerance_seconds=SEGMENT_JOIN_TOLERANCE_SECONDS), start, end
    )
    return parts, gaps, truncated


async def build_export_plan(
    session: AsyncSession,
    camera_id: str,
    start: datetime,
    end: datetime,
    *,
    now: datetime | None = None,
) -> ExportPlan:
    """The ordered segments covering a range, with the trims to apply.

    Every part is a stream copy of part of one file, so a later export can
    concatenate without re-encoding. A range made of more parts than
    ``MAX_EXPORT_PARTS`` is refused outright rather than silently truncated:
    an export that quietly stops partway through would be a far worse surprise
    than an error asking for a shorter range.
    """
    start, end = check_range(start, end, max_seconds=MAX_EXPORT_SECONDS)
    moment = now or datetime.now(UTC)

    parts, gaps, truncated = await plan_parts(
        session, camera_id, start, end, now=moment, limit=MAX_EXPORT_PARTS
    )
    if truncated:
        raise TimelineRangeError("That range is made of too many segments to export in one file.")

    total = sum(part.duration_seconds for part in parts)
    estimated = sum(part.size_bytes for part in parts)
    return ExportPlan(
        camera_id=camera_id,
        start=start,
        end=end,
        parts=parts,
        gaps=gaps,
        total_seconds=total,
        estimated_bytes=estimated,
    )
