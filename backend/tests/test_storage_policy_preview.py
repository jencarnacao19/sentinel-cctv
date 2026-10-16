"""Policy saves require a measured review; preview itself never deletes footage."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.database.models import RecordingSegment, SegmentStatus
from app.database.session import session_scope
from app.storage import cleanup, policy_preview, retention
from app.storage.metrics import collect_recording_telemetry, estimate_retention_days
from app.storage.root import StorageStatus
from app.system import settings_store
from tests.test_storage import CAMERA_PAYLOAD, make_segment, segment_count

GIB = 1024**3
MEASURED = StorageStatus(
    path=Path("/recordings"),
    available=True,
    total_bytes=394 * GIB,
    used_bytes=370 * GIB,
    free_bytes=4 * GIB,
)


@pytest.fixture
def measured_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.storage.router.check_storage", lambda _: MEASURED)


async def review(client: AsyncClient, proposed: dict) -> dict:
    response = await client.post("/api/v1/storage/settings/preview", json=proposed)
    assert response.status_code == 200, response.text
    body = response.json()
    return {
        **proposed,
        "preview_token": body["preview_token"],
        "confirmation": body["required_confirmation"],
    }


async def test_preview_and_confirmed_save_never_delete(
    admin_client: AsyncClient,
    recordings_dir: Path,
    measured_disk: None,
) -> None:
    camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
    eligible = await make_segment(recordings_dir, camera["id"], age_hours=240, size=1000)
    protected = await make_segment(
        recordings_dir,
        camera["id"],
        age_hours=720,
        size=2000,
        protected=True,
    )
    active = await make_segment(recordings_dir, camera["id"], age_hours=241, size=3000)
    async with session_scope() as session:
        row = await session.get(RecordingSegment, active.id)
        assert row
        row.status = SegmentStatus.RECORDING
        row.ended_at = None
    proposal = {"retention_days": 7, "min_free_bytes": 20 * GIB}
    refused = await admin_client.patch("/api/v1/storage/settings", json=proposal)
    assert refused.status_code == 409
    before = (await admin_client.get("/api/v1/storage/settings")).json()
    assert before["min_free_bytes"] == 5 * GIB
    response = await admin_client.post("/api/v1/storage/settings/preview", json=proposal)
    assert response.status_code == 200
    body = response.json()
    assert body["segment_count"] == body["expired_segment_count"] == 1
    assert body["estimated_bytes"] == 1000
    assert body["protected_count"] == body["active_or_recent_count"] == 1
    assert body["free_bytes"] == 4 * GIB
    assert body["reserve_shortfall_bytes"] == 16 * GIB
    assert body["remaining_shortfall_bytes"] == 16 * GIB - 1000
    assert body["saved_clips_preserved"] is True
    assert body["estimated_retention_days"] is None
    assert (
        await admin_client.patch(
            "/api/v1/storage/settings",
            json={
                **proposal,
                "preview_token": body["preview_token"],
            },
        )
    ).status_code == 409
    accepted = await admin_client.patch(
        "/api/v1/storage/settings",
        json={
            **proposal,
            "preview_token": body["preview_token"],
            "confirmation": body["required_confirmation"],
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["min_free_bytes"] == 20 * GIB
    assert await segment_count() == 3
    assert all((recordings_dir / row.path).exists() for row in (eligible, protected, active))


@pytest.mark.parametrize("change", ["proposal", "selection", "baseline", "expired"])
async def test_confirmation_is_bound_to_policy_footage_and_time(
    admin_client: AsyncClient,
    recordings_dir: Path,
    measured_disk: None,
    change: str,
) -> None:
    camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
    await make_segment(recordings_dir, camera["id"], age_hours=240, size=100)
    payload = await review(admin_client, {"retention_days": 7})
    if change == "proposal":
        payload["retention_days"] = 3
    elif change == "selection":
        await make_segment(recordings_dir, camera["id"], age_hours=241, size=100)
    elif change == "baseline":
        async with session_scope() as session:
            await settings_store.set_setting(session, settings_store.STORAGE_RETENTION_DAYS, 30)
    else:
        token = payload["preview_token"]
        expires, fingerprint = policy_preview._reviews[token]
        policy_preview._reviews[token] = (expires - 301, fingerprint)
    refused = await admin_client.patch("/api/v1/storage/settings", json=payload)
    assert refused.status_code == 409


async def test_impossible_or_unavailable_volume_cannot_be_confirmed(
    admin_client: AsyncClient,
    measured_disk: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        await admin_client.post(
            "/api/v1/storage/settings/preview",
            json={
                "min_free_bytes": MEASURED.total_bytes,
            },
        )
    ).status_code == 422
    monkeypatch.setattr(
        "app.storage.router.check_storage",
        lambda _: replace(MEASURED, available=False),
    )
    assert (
        await admin_client.post(
            "/api/v1/storage/settings/preview",
            json={
                "retention_days": 7,
            },
        )
    ).status_code == 409
    assert (
        await admin_client.patch(
            "/api/v1/storage/settings",
            json={
                "retention_days": 7,
                "confirmation": policy_preview.CONFIRMATION,
            },
        )
    ).status_code == 409


async def test_active_old_and_recently_finished_segments_survive_all_cleanup(
    admin_client: AsyncClient,
    recordings_dir: Path,
) -> None:
    camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
    active = await make_segment(recordings_dir, camera["id"], age_hours=240, size=100)
    finished_now = await make_segment(recordings_dir, camera["id"], age_hours=241, size=100)
    async with session_scope() as session:
        first = await session.get(RecordingSegment, active.id)
        second = await session.get(RecordingSegment, finished_now.id)
        assert first and second
        first.status = SegmentStatus.RECORDING
        first.ended_at = None
        second.ended_at = datetime.now(UTC) - timedelta(seconds=10)
    async with session_scope() as session:
        explicit = await cleanup.preview(session, cleanup.CleanupSelection())
        assert explicit.segment_count == 1  # Explicit cleanup includes finished footage only.
        result = await retention.sweep(
            session,
            recordings_dir,
            policy=retention.RetentionPolicy(retention_days=1, max_bytes=1, min_free_bytes=None),
            storage=MEASURED,
        )
    assert result.deleted == 0
    assert await segment_count() == 2
    assert (recordings_dir / active.path).exists()
    assert (recordings_dir / finished_now.path).exists()


async def test_rate_ignores_active_and_future_segments(
    admin_client: AsyncClient,
    recordings_dir: Path,
) -> None:
    camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
    active = await make_segment(recordings_dir, camera["id"], age_hours=1, size=100)
    await make_segment(recordings_dir, camera["id"], age_hours=-1, size=100)
    async with session_scope() as session:
        row = await session.get(RecordingSegment, active.id)
        assert row
        row.status = SegmentStatus.RECORDING
        row.ended_at = None
    async with session_scope() as session:
        telemetry = await collect_recording_telemetry(session, include_cameras=True)
    assert telemetry.recording_rate_bytes_per_hour is None
    assert telemetry.cameras[0].recording_rate_bytes_per_hour is None


def test_capacity_subtracts_reserve_deficit_protection_and_size_limit() -> None:
    days, remaining = estimate_retention_days(
        recordings_bytes=300 * GIB,
        free_bytes=4 * GIB,
        min_free_bytes=20 * GIB,
        protected_bytes=40 * GIB,
        max_bytes=200 * GIB,
        recording_rate_bytes_per_hour=GIB,
    )
    assert days == pytest.approx(160 / 24)
    assert remaining == 0


async def test_preview_requires_admin(client: AsyncClient) -> None:
    assert (await client.post("/api/v1/storage/settings/preview", json={})).status_code == 401


async def test_preview_honours_camera_age_override_without_double_counting_capacity(
    admin_client: AsyncClient,
    recordings_dir: Path,
    measured_disk: None,
) -> None:
    ordinary = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
    overridden = (
        await admin_client.post(
            "/api/v1/cameras",
            json={
                **CAMERA_PAYLOAD,
                "name": "Long retention",
                "retention_days": 60,
            },
        )
    ).json()
    await make_segment(recordings_dir, ordinary["id"], age_hours=480, size=100)
    await make_segment(recordings_dir, overridden["id"], age_hours=481, size=200)
    age = await admin_client.post(
        "/api/v1/storage/settings/preview",
        json={
            "retention_days": 7,
            "min_free_bytes": 0,
        },
    )
    assert age.json()["segment_count"] == 1
    assert age.json()["estimated_bytes"] == 100
    capacity = await admin_client.post(
        "/api/v1/storage/settings/preview",
        json={
            "retention_days": 7,
            "min_free_bytes": 20 * GIB,
        },
    )
    assert capacity.json()["segment_count"] == 2
    assert capacity.json()["expired_segment_count"] == 1
    assert capacity.json()["estimated_bytes"] == 300


async def test_protection_changed_after_selection_is_checked_before_unlink(
    admin_client: AsyncClient,
    recordings_dir: Path,
) -> None:
    camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
    segment = await make_segment(recordings_dir, camera["id"], age_hours=240, size=100)
    async with session_scope() as selected_session:
        selected = await selected_session.get(RecordingSegment, segment.id)
        assert selected
        async with session_scope() as writer:
            row = await writer.get(RecordingSegment, segment.id)
            assert row
            row.protected = True
        assert await retention._delete_segment(selected_session, selected, recordings_dir) is None
    assert (recordings_dir / segment.path).exists()


async def test_age_sweep_does_not_starve_behind_camera_overrides(
    admin_client: AsyncClient,
    recordings_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
    overridden = (
        await admin_client.post(
            "/api/v1/cameras",
            json={
                **CAMERA_PAYLOAD,
                "name": "Long retention",
                "retention_days": 60,
            },
        )
    ).json()
    kept = await make_segment(recordings_dir, overridden["id"], age_hours=481, size=200)
    expired = await make_segment(recordings_dir, ordinary["id"], age_hours=480, size=100)
    monkeypatch.setattr(retention, "MAX_DELETIONS_PER_SWEEP", 1)
    async with session_scope() as session:
        result = await retention.sweep(
            session,
            recordings_dir,
            policy=retention.RetentionPolicy(retention_days=7, max_bytes=None, min_free_bytes=None),
            storage=MEASURED,
        )
    assert result.deleted == 1
    assert (recordings_dir / kept.path).exists()
    assert not (recordings_dir / expired.path).exists()
