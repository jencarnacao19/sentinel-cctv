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

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RecordingSegment
from app.logging_config import get_logger
from app.recording import paths, timeline
from app.recording.fragments import FragmentIndex, FragmentIndexError, index_fragments
from app.recording.timeline import ExportPart, PlaybackTarget, TimeSpan
from app.security.paths import UnsafePathError, resolve_within

logger = get_logger("RECORDING")

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


# --- HLS playlist -----------------------------------------------------------

#: Footage one playlist covers. At four-second chunks an hour is about 900
#: entries; the player opens the next hour when it reaches the end of this one.
PLAYLIST_WINDOW_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class PlaylistItem:
    """One recorded file's stretch of the playlist."""

    segment_id: str
    started_at: datetime
    """Wall-clock time of the item's first chunk."""
    position: float
    """Where the item starts on the playlist's own timeline, in seconds."""
    duration: float


@dataclass(frozen=True, slots=True)
class PlaybackPlaylist:
    """A VOD playlist that addresses recorded files by byte range.

    ``files`` is the only set of files a grant for this playlist may serve.
    """

    text: str
    files: dict[str, Path]
    items: list[PlaylistItem]
    start_position: float
    """Seconds into the playlist where the requested instant is."""
    end: datetime | None
    """Wall-clock end of the last item: where the next playlist starts."""
    next_at: datetime | None
    """When the playlist is empty, the next instant with footage, if any."""


def _index_all(files: list[Path]) -> list[FragmentIndex | None]:
    indexes: list[FragmentIndex | None] = []
    for file in files:
        try:
            indexes.append(index_fragments(file))
        except (FragmentIndexError, OSError):
            # An unreadable file is left out, which the player shows as a cut.
            logger.warning("Recording cannot be streamed", extra={"file": file.name})
            indexes.append(None)
    return indexes


def _render(entries: list[tuple[PlaylistItem, int, list[tuple[int, int, float]]]]) -> str:
    longest = max(seconds for _, _, chunks in entries for _, _, seconds in chunks)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{max(math.ceil(longest), 1)}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for number, (item, init_length, chunks) in enumerate(entries):
        uri = f"{item.segment_id}.mp4"
        # Every file restarts its own timestamps and carries its own header.
        if number:
            lines.append("#EXT-X-DISCONTINUITY")
        stamp = item.started_at.astimezone(UTC).isoformat(timespec="milliseconds")
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp.replace('+00:00', 'Z')}")
        lines.append(f'#EXT-X-MAP:URI="{uri}",BYTERANGE="{init_length}@0"')
        for offset, length, seconds in chunks:
            lines += [f"#EXTINF:{seconds:.3f},", f"#EXT-X-BYTERANGE:{length}@{offset}", uri]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


async def build_playlist(
    session: AsyncSession,
    camera_id: str,
    at: datetime,
    *,
    root: Path,
    now: datetime | None = None,
) -> PlaybackPlaylist:
    """An HLS playlist of up to an hour of footage starting at ``at``.

    Chunks are whole keyframe fragments, so the first one may begin a few
    seconds before ``at``; ``start_position`` says how far into it to start.
    """
    moment = now or datetime.now(UTC)
    at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    end = min(at + timedelta(seconds=PLAYLIST_WINDOW_SECONDS), moment)
    parts: list[ExportPart] = []
    if end > at:
        parts, _, _ = await timeline.plan_parts(
            session, camera_id, at, end, now=moment, limit=MAX_MANIFEST_PARTS
        )

    rows = await session.execute(
        select(RecordingSegment.id, RecordingSegment.path).where(
            RecordingSegment.id.in_([part.segment_id for part in parts])
        )
    )
    files: dict[str, Path] = {}
    for segment_id, relative in rows:
        try:
            files[segment_id] = resolve_within(paths.absolute_path(root, relative), root)
        except (UnsafePathError, OSError):
            logger.error(
                "Recording path escapes the storage root", extra={"segment_id": segment_id}
            )
    parts = [part for part in parts if part.segment_id in files]
    indexes = await asyncio.to_thread(_index_all, [files[part.segment_id] for part in parts])

    entries: list[tuple[PlaylistItem, int, list[tuple[int, int, float]]]] = []
    position = 0.0
    for part, index in zip(parts, indexes, strict=True):
        if index is None:
            continue
        file_start = part.started_at - timedelta(seconds=part.trim_start_seconds)
        chunks: list[tuple[int, int, float]] = []
        first: datetime | None = None
        elapsed = 0.0
        for chunk in index.chunks:
            chunk_start = file_start + timedelta(seconds=elapsed)
            elapsed += chunk.seconds
            if file_start + timedelta(seconds=elapsed) <= part.started_at:
                continue
            if chunk_start >= part.ended_at:
                break
            first = first or chunk_start
            chunks.append((chunk.offset, chunk.length, chunk.seconds))
        if first is None:
            continue
        duration = sum(seconds for _, _, seconds in chunks)
        item = PlaylistItem(part.segment_id, first, position, duration)
        entries.append((item, index.init_length, chunks))
        position += duration

    if not entries:
        target = await timeline.resolve_playback(session, camera_id, at, now=moment)
        return PlaybackPlaylist("", {}, [], 0.0, None, target.next_started_at)

    items = [item for item, _, _ in entries]
    last = items[-1]
    return PlaybackPlaylist(
        text=_render(entries),
        files={item.segment_id: files[item.segment_id] for item in items},
        items=items,
        start_position=max(0.0, (at - items[0].started_at).total_seconds()),
        end=last.started_at + timedelta(seconds=last.duration),
        next_at=None,
    )
