"""Layout of the recordings directory.

::

    <root>/<camera-id>/2026/08/13/15/segment-15-00-00.mp4

The layout exists so a human browsing the disk can find footage without the
database. The database remains the index -- nothing reads meaning out of a
filename except the indexer, and even it cross-checks what it parses.

All times in the path are UTC. The recorder runs FFmpeg with ``TZ=UTC`` so its
``strftime`` expansion cannot drift with the host's timezone or with a daylight
saving change, which would otherwise produce an hour of overwritten or
out-of-order filenames twice a year.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: FFmpeg segment muxer pattern, expanded by its own strftime.
SEGMENT_PATTERN = "%Y/%m/%d/%H/segment-%H-%M-%S.mp4"

SEGMENT_SUFFIX = ".mp4"

_SEGMENT_RE = re.compile(
    r"(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/(?P<hour>\d{2})/"
    r"segment-(?P<h>\d{2})-(?P<m>\d{2})-(?P<s>\d{2})(?:_(?P<seq>\d+))?\.mp4$"
)


def camera_directory(root: Path, camera_id: str) -> Path:
    return root / camera_id


def output_pattern(root: Path, camera_id: str) -> str:
    """The full strftime pattern handed to FFmpeg's segment muxer."""
    return str(camera_directory(root, camera_id) / SEGMENT_PATTERN)


def relative_path(root: Path, path: Path) -> str:
    """Store paths relative to the root, so moving the storage does not
    invalidate every row in the database."""
    return path.resolve().relative_to(root.resolve()).as_posix()


def absolute_path(root: Path, relative: str) -> Path:
    return root / relative


def parse_segment_start(relative: str) -> datetime | None:
    """Recover a segment's UTC start time from its path.

    Returns ``None`` for anything that does not match the layout, so a stray
    file in the recordings directory is ignored rather than indexed with a
    nonsense timestamp.
    """
    match = _SEGMENT_RE.search(relative.replace("\\", "/"))
    if match is None:
        return None
    try:
        return datetime(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            int(match["h"]),
            int(match["m"]),
            int(match["s"]),
            tzinfo=UTC,
        )
    except ValueError:
        # A filename that parses structurally but is not a real date, such as
        # an hour of 25 from a corrupted name.
        return None


def camera_id_from_relative(relative: str) -> str | None:
    """The camera directory is always the first path component."""
    parts = relative.replace("\\", "/").split("/")
    return parts[0] if len(parts) > 1 else None


def hour_directory(root: Path, camera_id: str, when: datetime) -> Path:
    moment = when.astimezone(UTC)
    return (
        camera_directory(root, camera_id)
        / f"{moment.year:04d}"
        / f"{moment.month:02d}"
        / f"{moment.day:02d}"
        / f"{moment.hour:02d}"
    )


def ensure_hour_directories(
    root: Path, camera_id: str, when: datetime, *, hours_ahead: int = 2
) -> list[Path]:
    """Pre-create the hour directories FFmpeg is about to write into.

    The segment muxer expands ``strftime`` in the output path but, unlike the
    image2 and hls muxers, will not create the directories -- it simply fails
    when the clock rolls into an hour that does not exist yet. Creating a couple
    of hours ahead means a recorder never stops on the hour boundary.
    """
    created: list[Path] = []
    for offset in range(hours_ahead + 1):
        moment = when.astimezone(UTC) + timedelta(hours=offset)
        directory = hour_directory(root, camera_id, moment)
        directory.mkdir(parents=True, exist_ok=True)
        created.append(directory)
    return created


def iter_segment_files(root: Path, camera_id: str) -> list[Path]:
    """Every segment file for one camera, oldest path first.

    Sorted lexicographically, which for this zero-padded layout is also
    chronological.
    """
    directory = camera_directory(root, camera_id)
    if not directory.is_dir():
        return []
    return sorted(directory.rglob(f"*{SEGMENT_SUFFIX}"))
