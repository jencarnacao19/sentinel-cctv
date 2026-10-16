"""Snapshots and thumbnails for motion events.

A greyscale 640x360 analysis frame would make a poor snapshot, so the image
comes from go2rtc in colour at full resolution. The thumbnail is what the
timeline and the events list actually load: hundreds of full snapshots on one
page would be tens of megabytes for images displayed 160 pixels wide.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from app.config import Settings
from app.logging_config import get_logger

logger = get_logger("MOTION")

THUMBNAIL_WIDTH = 320
THUMBNAIL_QUALITY = 70
SNAPSHOT_QUALITY = 85


@dataclass(frozen=True, slots=True)
class SavedSnapshot:
    snapshot_path: str
    thumbnail_path: str


def event_relative_paths(camera_id: str, event_id: str, when: datetime) -> tuple[str, str]:
    """Nested by day so a directory never holds a year of files."""
    day = f"{when:%Y/%m/%d}"
    stem = f"{camera_id}/{day}/{event_id}"
    return f"{stem}.jpg", f"{stem}-thumb.jpg"


def _encode(image: np.ndarray, quality: int) -> bytes | None:
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buffer.tobytes() if ok else None


def _write_snapshot_files(
    settings: Settings, snapshot_relative: str, thumbnail_relative: str, image_bytes: bytes
) -> bool:
    """Blocking; always called through a thread."""
    decoded = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        return False

    snapshot_path = settings.snapshots_dir / snapshot_relative
    thumbnail_path = settings.thumbnails_dir / thumbnail_relative
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)

    # The original is re-encoded rather than written through: a camera's own
    # JPEG can be several megabytes, and nothing here needs that.
    full = _encode(decoded, SNAPSHOT_QUALITY)
    if full is None:
        return False
    snapshot_path.write_bytes(full)

    height, width = decoded.shape[:2]
    if width > THUMBNAIL_WIDTH:
        scale = THUMBNAIL_WIDTH / width
        decoded = cv2.resize(
            decoded,
            (THUMBNAIL_WIDTH, max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    thumbnail = _encode(decoded, THUMBNAIL_QUALITY)
    if thumbnail is None:
        return False
    thumbnail_path.write_bytes(thumbnail)
    return True


async def save_snapshot(
    settings: Settings, camera_id: str, event_id: str, when: datetime, image_bytes: bytes
) -> SavedSnapshot | None:
    """Write the snapshot and its thumbnail. Returns their relative paths."""
    snapshot_relative, thumbnail_relative = event_relative_paths(camera_id, event_id, when)
    try:
        written = await asyncio.to_thread(
            _write_snapshot_files, settings, snapshot_relative, thumbnail_relative, image_bytes
        )
    except OSError:
        logger.warning("Could not write an event snapshot", extra={"event_id": event_id})
        return None

    if not written:
        return None
    return SavedSnapshot(snapshot_path=snapshot_relative, thumbnail_path=thumbnail_relative)


def remove_snapshot(settings: Settings, snapshot: str | None, thumbnail: str | None) -> None:
    """Blocking; used by retention when an event is deleted."""
    for base, relative in (
        (settings.snapshots_dir, snapshot),
        (settings.thumbnails_dir, thumbnail),
    ):
        if not relative:
            continue
        try:
            Path(base / relative).unlink(missing_ok=True)
        except OSError:
            continue
