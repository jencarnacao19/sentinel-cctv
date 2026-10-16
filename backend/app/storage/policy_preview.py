"""Read-only, cumulative retention impact and short-lived operator reviews.

Bytes come from the index, not filesystem allocation. A preview describes all
currently eligible rows across bounded sweeps, not a promise about a single pass.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Camera, MotionEvent, RecordingSegment, SegmentStatus
from app.storage.metrics import collect_recording_telemetry, estimate_retention_days
from app.storage.retention import MINIMUM_AGE_SECONDS, RetentionPolicy, candidate_filters
from app.storage.root import StorageStatus

CONFIRMATION = "APPLY RETENTION POLICY"
REVIEW_SECONDS = 300
_reviews: dict[str, tuple[float, str]] = {}


class PolicyImpact(BaseModel):
    segment_count: int
    estimated_bytes: int
    expired_segment_count: int
    expired_event_count: int
    protected_count: int
    protected_bytes: int
    active_or_recent_count: int
    free_bytes: int
    reserve_shortfall_bytes: int
    estimated_free_bytes_after: int
    remaining_shortfall_bytes: int
    recording_rate_bytes_per_hour: float | None
    rate_window_hours: float | None
    estimated_retention_days: float | None
    estimated_days_remaining: float | None
    oldest_affected_recording: datetime | None
    newest_affected_recording: datetime | None
    saved_clips_preserved: bool = True
    warnings: list[str]


def needs_review(current: dict, proposed: dict) -> bool:
    """A tighter policy can authorize future deletion even with no current rows."""
    old_days, new_days = current["retention_days"], proposed["retention_days"]
    old_max, new_max = current["max_bytes"], proposed["max_bytes"]
    return bool(
        (new_days is not None and (old_days is None or new_days < old_days))
        or (new_max is not None and (old_max is None or new_max < old_max))
        or (proposed["min_free_bytes"] or 0) > (current["min_free_bytes"] or 0)
    )


def remember_review(fingerprint: str) -> str:
    now = time.monotonic()
    for token, (expires, _) in list(_reviews.items()):
        if expires < now:
            _reviews.pop(token, None)
    if len(_reviews) >= 256:
        _reviews.pop(next(iter(_reviews)))
    token = secrets.token_urlsafe(24)
    _reviews[token] = (now + REVIEW_SECONDS, fingerprint)
    return token


def consume_review(token: str | None, fingerprint: str) -> bool:
    review = _reviews.pop(token or "", None)
    return bool(review and review[0] >= time.monotonic() and review[1] == fingerprint)


async def preview_policy(
    session: AsyncSession,
    storage: StorageStatus,
    current: dict,
    proposed: dict,
    *,
    now: datetime | None = None,
) -> tuple[PolicyImpact, str]:
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(seconds=MINIMUM_AGE_SECONDS)
    policy = RetentionPolicy(
        retention_days=proposed["retention_days"],
        max_bytes=proposed["max_bytes"],
        min_free_bytes=proposed["min_free_bytes"],
    )
    overrides = dict(
        (await session.execute(select(Camera.id, Camera.retention_days))).tuples().all()
    )
    telemetry = await collect_recording_telemetry(session, now=moment)
    candidates = (
        await session.execute(
            select(
                RecordingSegment.id,
                RecordingSegment.camera_id,
                RecordingSegment.started_at,
                RecordingSegment.ended_at,
                RecordingSegment.size_bytes,
            )
            .where(*candidate_filters(cutoff))
            .order_by(RecordingSegment.started_at, RecordingSegment.id)
        )
    ).all()
    selected: dict[str, tuple[int, datetime, datetime]] = {}
    if policy.retention_days:
        for row in candidates:
            days = overrides.get(row.camera_id) or policy.retention_days
            if row.started_at < moment - timedelta(days=days):
                selected[row.id] = (row.size_bytes, row.started_at, row.ended_at)
    expired_count = len(selected)
    freed = sum(row[0] for row in selected.values())
    reserve_shortfall = max((policy.min_free_bytes or 0) - storage.free_bytes, 0)
    size_shortfall = (
        max(telemetry.recordings_bytes - policy.max_bytes, 0) if policy.max_bytes else 0
    )
    target = max(reserve_shortfall, size_shortfall)
    for row in candidates:
        if freed >= target:
            break
        if row.id not in selected:
            selected[row.id] = (row.size_bytes, row.started_at, row.ended_at)
            freed += row.size_bytes

    event_ids: list[str] = []
    if policy.retention_days:
        events = await session.execute(
            select(MotionEvent.id, MotionEvent.camera_id, MotionEvent.started_at).where(
                MotionEvent.protected.is_(False),
                MotionEvent.started_at < cutoff,
                MotionEvent.ended_at.is_not(None),
                MotionEvent.ended_at < cutoff,
            )
        )
        for event in events:
            days = overrides.get(event.camera_id) or policy.retention_days
            if event.started_at < moment - timedelta(days=days):
                event_ids.append(event.id)

    inactive_count = await session.scalar(
        select(func.count(RecordingSegment.id)).where(
            RecordingSegment.protected.is_(False),
            RecordingSegment.status != SegmentStatus.MISSING,
        )
    )
    estimated_days, remaining_days = estimate_retention_days(
        recordings_bytes=telemetry.recordings_bytes,
        free_bytes=storage.free_bytes,
        min_free_bytes=policy.min_free_bytes,
        max_bytes=policy.max_bytes,
        protected_bytes=telemetry.protected_bytes,
        recording_rate_bytes_per_hour=telemetry.recording_rate_bytes_per_hour,
    )
    warnings = [
        "Estimate from indexed file sizes; actual freed disk space may differ. "
        "Selection covers subsequent bounded sweeps and may change as recording continues.",
        "Protected recordings, active/recent footage and saved exports are excluded. "
        "Camera retention overrides still apply to age; capacity limits can remove "
        "younger footage.",
        "Expired event metadata and its images follow the age policy. The existing 24-hour "
        "motion-image cleanup is unchanged and its bytes are excluded from this estimate.",
    ]
    if target > freed:
        warnings.append("Eligible footage cannot satisfy the requested reserve or storage limit.")
    if telemetry.recording_rate_bytes_per_hour is None:
        warnings.append("No recent completed footage is available to estimate recording days.")
    elif (
        estimated_days is not None
        and policy.retention_days
        and estimated_days < policy.retention_days
    ):
        warnings.append("Measured recording rate cannot fit the requested retention period.")
    impact = PolicyImpact(
        segment_count=len(selected),
        estimated_bytes=freed,
        expired_segment_count=expired_count,
        expired_event_count=len(event_ids),
        protected_count=telemetry.protected_count,
        protected_bytes=telemetry.protected_bytes,
        active_or_recent_count=max(int(inactive_count or 0) - len(candidates), 0),
        free_bytes=storage.free_bytes,
        reserve_shortfall_bytes=reserve_shortfall,
        estimated_free_bytes_after=storage.free_bytes + freed,
        remaining_shortfall_bytes=max(target - freed, 0),
        recording_rate_bytes_per_hour=telemetry.recording_rate_bytes_per_hour,
        rate_window_hours=telemetry.rate_window_hours,
        estimated_retention_days=estimated_days,
        estimated_days_remaining=remaining_days,
        oldest_affected_recording=min((row[1] for row in selected.values()), default=None),
        newest_affected_recording=max((row[2] for row in selected.values()), default=None),
        warnings=warnings,
    )
    # Bind confirmation to both policies and the exact affected rows/sizes.
    # A changed policy in another tab or an expanded selection requires review.
    fingerprint = hashlib.sha256(
        json.dumps(
            [current, proposed, sorted(selected.items()), sorted(event_ids)],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    return impact, fingerprint
