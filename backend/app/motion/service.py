"""Persistence of motion events, and marking the segments they overlap.

Kept separate from the detector and the aggregator so both stay pure and
testable, and so the timeline's "which segments contain motion" question is
answered by one place.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Camera, MotionEvent, RecordingSegment
from app.events.bus import MOTION_ENDED, MOTION_STARTED, publish
from app.logging_config import get_logger
from app.motion.aggregator import AggregatorConfig, MotionWindow
from app.system import settings_store

logger = get_logger("MOTION")


async def load_aggregator_config(session: AsyncSession) -> AggregatorConfig:
    values = await settings_store.get_many(
        session,
        [
            settings_store.MOTION_END_GRACE_SECONDS,
            settings_store.MOTION_PRE_ROLL_SECONDS,
            settings_store.MOTION_POST_ROLL_SECONDS,
        ],
    )
    return AggregatorConfig(
        end_grace_seconds=float(values[settings_store.MOTION_END_GRACE_SECONDS]),
        pre_roll_seconds=float(values[settings_store.MOTION_PRE_ROLL_SECONDS]),
        post_roll_seconds=float(values[settings_store.MOTION_POST_ROLL_SECONDS]),
    )


def parse_zones(raw: str | None) -> list[list[tuple[float, float]]]:
    """Decode a camera's ignore-polygons, tolerating anything malformed.

    A bad value must not stop motion detection for that camera; the worst
    outcome of ignoring it is more events, not none.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Motion zones could not be decoded; ignoring them")
        return []
    if not isinstance(data, list):
        return []

    zones: list[list[tuple[float, float]]] = []
    for polygon in data:
        if not isinstance(polygon, list) or len(polygon) < 3:
            continue
        points: list[tuple[float, float]] = []
        for point in polygon:
            if (
                isinstance(point, (list, tuple))
                and len(point) == 2
                and all(isinstance(value, (int, float)) for value in point)
            ):
                points.append((float(point[0]), float(point[1])))
        if len(points) >= 3:
            zones.append(points)
    return zones


def serialise_zones(zones: list[list[tuple[float, float]]]) -> str:
    return json.dumps([[[round(x, 4), round(y, 4)] for x, y in polygon] for polygon in zones])


async def start_event(
    session: AsyncSession, camera: Camera, window: MotionWindow, config: AggregatorConfig
) -> MotionEvent:
    pre_roll_start, _ = (
        window.started_at - timedelta(seconds=config.pre_roll_seconds),
        None,
    )
    event = MotionEvent(
        camera_id=camera.id,
        started_at=window.started_at,
        peak_score=window.peak_score,
        pre_roll_start=pre_roll_start,
    )
    session.add(event)
    await session.flush()
    logger.info(
        "Motion started",
        extra={"camera": camera.name, "camera_id": camera.id, "event_id": event.id},
    )
    publish(
        MOTION_STARTED,
        {
            "camera_id": camera.id,
            "camera": camera.name,
            "event_id": event.id,
            "started_at": event.started_at,
        },
    )
    return event


async def update_peak(session: AsyncSession, event: MotionEvent, score: float) -> None:
    if score > event.peak_score:
        event.peak_score = score
        await session.flush()


async def finish_event(
    session: AsyncSession,
    event: MotionEvent,
    window: MotionWindow,
    config: AggregatorConfig,
) -> MotionEvent:
    ended = window.ended_at or window.last_motion_at
    event.ended_at = ended
    event.duration_seconds = max(0.0, (ended - event.started_at).total_seconds())
    event.post_roll_end = ended + timedelta(seconds=config.post_roll_seconds)
    event.peak_score = max(event.peak_score, window.peak_score)
    await session.flush()

    await mark_segments_with_motion(session, event)

    logger.info(
        "Motion ended",
        extra={
            "camera_id": event.camera_id,
            "event_id": event.id,
            "duration_seconds": round(event.duration_seconds, 1),
        },
    )
    publish(
        MOTION_ENDED,
        {
            "camera_id": event.camera_id,
            "event_id": event.id,
            "duration_seconds": event.duration_seconds,
            "peak_score": event.peak_score,
        },
    )
    return event


async def mark_segments_with_motion(session: AsyncSession, event: MotionEvent) -> int:
    """Flag the recording segments this event overlaps.

    This is what lets the timeline shade the periods containing movement without
    joining against every event on every draw.
    """
    if event.ended_at is None:
        return 0

    rows = await session.execute(
        select(RecordingSegment).where(
            RecordingSegment.camera_id == event.camera_id,
            RecordingSegment.started_at < event.ended_at,
            # A segment still being written has no end yet but may well overlap.
            (RecordingSegment.ended_at.is_(None)) | (RecordingSegment.ended_at > event.started_at),
        )
    )

    touched = 0
    for segment in rows.scalars():
        segment_end = segment.ended_at or event.ended_at
        overlap = (
            min(segment_end, event.ended_at) - max(segment.started_at, event.started_at)
        ).total_seconds()
        if overlap <= 0:
            continue
        segment.has_motion = True
        segment.motion_seconds = round(segment.motion_seconds + overlap, 2)
        touched += 1

    if touched:
        await session.flush()
    return touched


async def close_orphaned_events(session: AsyncSession) -> int:
    """Close events left open by a crash or an unclean shutdown.

    An event with no end time would be drawn by the timeline as still running,
    for ever.
    """
    now = datetime.now(UTC)
    rows = await session.execute(select(MotionEvent).where(MotionEvent.ended_at.is_(None)))
    orphans = list(rows.scalars())
    if not orphans:
        return 0

    for event in orphans:
        event.ended_at = event.started_at
        event.duration_seconds = 0.0
        event.post_roll_end = event.started_at
    await session.flush()

    logger.warning(
        "Closed motion events left open by an unclean shutdown",
        extra={"count": len(orphans), "at": now.isoformat()},
    )
    return len(orphans)


async def clear_segment_motion(session: AsyncSession, camera_id: str) -> None:
    """Used when motion is turned off for a camera, so the timeline stops
    claiming movement nothing is detecting any more."""
    await session.execute(
        update(RecordingSegment)
        .where(RecordingSegment.camera_id == camera_id)
        .values(has_motion=False, motion_seconds=0.0)
    )
    await session.flush()
