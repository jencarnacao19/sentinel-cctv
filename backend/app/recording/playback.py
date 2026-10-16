"""Continuous playback across segment boundaries.

A recording is never one file: it is sixty-second fragments, cut on clock
boundaries, each a small self-contained fragmented MP4. A player that only
knew how to open one segment would freeze or reset at every boundary, which is
exactly what an operator scrubbing back through last night must never see.

This module answers "what do I feed the player for this stretch of time?" as
an ordered manifest of segments plus the offset to seek to in the first one, so
the browser can fetch each file in turn and append it to the same
MediaSource ``SourceBuffer`` the live view already uses (see
``frontend/src/features/live/mse.ts``). A gap between two segments is reported
so the player can jump straight to the next item instead of stalling on an
empty buffer.

The manifest deliberately does not try to be exhaustive: a full day can be
thousands of one-minute files, and a player only ever needs the next stretch
of them. ``truncated`` tells the caller to ask again starting where this
response left off, the same pattern the timeline overview uses to cap its own
response size.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.recording import timeline
from app.recording.timeline import ExportPart, PlaybackTarget, TimeSpan

#: Per-request ceiling. Long enough that a player rarely needs a second
#: request mid-playback, short enough that the response is never the thing
#: that makes a scrub feel slow.
MAX_MANIFEST_SECONDS = 6 * 3600.0

#: At 60-second segments this covers six hours with room to spare; a request
#: that still overflows it is paged rather than silently cut short.
MAX_MANIFEST_PARTS = 2000


@dataclass(frozen=True, slots=True)
class ManifestItem:
    """One segment to append, and where in it to start.

    ``offset_seconds`` is non-zero only for the first item in a manifest: it is
    where the requested range begins inside that file, so the player can seek
    there once the appended data covers it rather than starting from the
    file's own beginning.
    """

    segment_id: str
    started_at: datetime
    ended_at: datetime
    offset_seconds: float
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class PlaybackManifest:
    camera_id: str
    start: datetime
    end: datetime
    items: list[ManifestItem]
    gaps: list[TimeSpan]
    """Stretches with no playable footage. The player skips forward to the next
    item's start rather than waiting on an empty buffer."""
    total_seconds: float
    truncated: bool
    """More footage matched than fit in one manifest; request again starting at
    the end of the last returned item (or gap) to continue."""


def _to_item(part: ExportPart) -> ManifestItem:
    return ManifestItem(
        segment_id=part.segment_id,
        started_at=part.started_at,
        ended_at=part.ended_at,
        offset_seconds=part.trim_start_seconds,
        duration_seconds=part.duration_seconds,
    )


async def build_manifest(
    session: AsyncSession,
    camera_id: str,
    start: datetime,
    end: datetime,
    *,
    now: datetime | None = None,
) -> PlaybackManifest:
    """The ordered segments to append for ``[start, end)``, and the gaps
    between them.

    Unlike an export, a manifest is never rejected for having no footage: an
    empty window is a legitimate answer (nothing recorded yet, or a gap the
    operator is scrubbing through) and the player is expected to handle it by
    jumping to whatever comes next, the same way it handles any other gap.
    """
    start, end = timeline.check_range(start, end, max_seconds=MAX_MANIFEST_SECONDS)
    moment = now or datetime.now(UTC)

    parts, gaps, truncated = await timeline.plan_parts(
        session, camera_id, start, end, now=moment, limit=MAX_MANIFEST_PARTS
    )
    items = [_to_item(part) for part in parts]
    total = sum(item.duration_seconds for item in items)

    return PlaybackManifest(
        camera_id=camera_id,
        start=start,
        end=end,
        items=items,
        gaps=gaps,
        total_seconds=total,
        truncated=truncated,
    )


async def resolve_seek(
    session: AsyncSession,
    camera_id: str,
    at: datetime,
    *,
    now: datetime | None = None,
) -> PlaybackTarget:
    """Where playback should jump to for a wall-clock seek.

    A thin pass-through to ``timeline.resolve_playback`` -- the query itself
    belongs with the rest of the timeline's segment lookups, but re-exporting
    it here means the router (and anything else driving the player) only needs
    to import this one module for everything a scrub requires.
    """
    return await timeline.resolve_playback(session, camera_id, at, now=now)
