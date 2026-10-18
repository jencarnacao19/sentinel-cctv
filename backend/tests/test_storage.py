"""Retention, protection and storage reporting."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.database.models import ExportJob, ExportStatus, RecordingSegment, SegmentStatus
from app.database.session import session_scope
from app.storage import retention
from app.storage.retention import RetentionPolicy
from app.storage.root import StorageStatus
from app.system import settings_store

CAMERA_PAYLOAD = {
    "name": "Porch",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
}

AVAILABLE = StorageStatus(
    path=Path("/recordings"),
    available=True,
    total_bytes=1_000_000_000,
    used_bytes=500_000_000,
    free_bytes=500_000_000,
)


async def make_segment(
    root: Path,
    camera_id: str,
    *,
    age_hours: float,
    size: int = 4_000_000,
    protected: bool = False,
) -> RecordingSegment:
    """Create a real file plus its row, so deletion can be observed on disk."""
    started = datetime.now(UTC) - timedelta(hours=age_hours)
    relative = (
        f"{camera_id}/{started:%Y/%m/%d/%H}/segment-{started:%H-%M-%S}-"
        f"{int(started.timestamp() * 1000) % 100000}.mp4"
    )
    absolute = root / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(b"\x00" * size)

    async with session_scope() as session:
        segment = RecordingSegment(
            camera_id=camera_id,
            path=relative,
            started_at=started,
            ended_at=started + timedelta(minutes=1),
            duration_seconds=60.0,
            size_bytes=size,
            protected=protected,
            status=SegmentStatus.COMPLETE,
        )
        session.add(segment)
        await session.flush()
        return segment


async def segment_count() -> int:
    async with session_scope() as session:
        rows = await session.execute(select(RecordingSegment))
        return len(list(rows.scalars()))


class TestAgeRetention:
    async def test_old_segments_are_deleted(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        old = await make_segment(recordings_dir, camera["id"], age_hours=24 * 20)
        recent = await make_segment(recordings_dir, camera["id"], age_hours=2)

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(retention_days=14, max_bytes=None, min_free_bytes=None),
                storage=AVAILABLE,
            )

        assert result.deleted == 1
        assert result.freed_bytes == 4_000_000
        assert not (recordings_dir / old.path).exists()
        assert (recordings_dir / recent.path).exists()
        assert await segment_count() == 1

    async def test_a_protected_segment_survives_its_retention_period(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        """Protection is absolute: it outranks every limit."""
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        kept = await make_segment(recordings_dir, camera["id"], age_hours=24 * 100, protected=True)

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(retention_days=1, max_bytes=None, min_free_bytes=None),
                storage=AVAILABLE,
            )

        assert result.deleted == 0
        assert (recordings_dir / kept.path).exists()
        assert await segment_count() == 1

    async def test_very_recent_footage_is_never_deleted(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        """A retention period of zero must not delete the segment currently
        being written."""
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await make_segment(recordings_dir, camera["id"], age_hours=0.005)

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(retention_days=1, max_bytes=None, min_free_bytes=None),
                storage=AVAILABLE,
            )
        assert result.deleted == 0

    async def test_a_per_camera_period_overrides_the_global_one(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (
            await admin_client.post(
                "/api/v1/cameras", json={**CAMERA_PAYLOAD, "retention_days": 60}
            )
        ).json()
        kept = await make_segment(recordings_dir, camera["id"], age_hours=24 * 20)

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(retention_days=7, max_bytes=None, min_free_bytes=None),
                storage=AVAILABLE,
            )

        assert result.deleted == 0
        assert (recordings_dir / kept.path).exists()

    async def test_a_cameras_own_period_applies_when_the_global_one_is_unlimited(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        """A short period on one camera is often a legal limit for that view."""
        limited = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        changed = await admin_client.patch(
            f"/api/v1/cameras/{limited['id']}", json={"retention_days": 3}
        )
        assert changed.status_code == 200, changed.text
        unlimited = (
            await admin_client.post(
                "/api/v1/cameras",
                json={
                    **CAMERA_PAYLOAD,
                    "name": "Second",
                    "main_stream_url": "rtsp://192.168.50.51/s",
                },
            )
        ).json()
        expired = await make_segment(recordings_dir, limited["id"], age_hours=24 * 5)
        kept = await make_segment(recordings_dir, unlimited["id"], age_hours=24 * 5)

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(retention_days=None, max_bytes=None, min_free_bytes=None),
                storage=AVAILABLE,
            )

        assert result.deleted == 1
        assert not (recordings_dir / expired.path).exists()
        assert (recordings_dir / kept.path).exists()


class TestSizeRetention:
    async def test_the_oldest_go_first_when_the_size_limit_is_exceeded(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        oldest = await make_segment(recordings_dir, camera["id"], age_hours=10, size=4_000_000)
        middle = await make_segment(recordings_dir, camera["id"], age_hours=5, size=4_000_000)
        newest = await make_segment(recordings_dir, camera["id"], age_hours=1, size=4_000_000)

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(
                    retention_days=None, max_bytes=5_000_000, min_free_bytes=None
                ),
                storage=AVAILABLE,
            )

        assert result.deleted == 2
        assert not (recordings_dir / oldest.path).exists()
        assert not (recordings_dir / middle.path).exists()
        assert (recordings_dir / newest.path).exists()

    async def test_protection_wins_over_the_size_limit(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        protected = await make_segment(
            recordings_dir, camera["id"], age_hours=10, size=8_000_000, protected=True
        )

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(
                    retention_days=None, max_bytes=1_000_000, min_free_bytes=None
                ),
                storage=AVAILABLE,
            )

        assert result.deleted == 0
        assert (recordings_dir / protected.path).exists()

    async def test_nothing_is_deleted_when_within_the_limit(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await make_segment(recordings_dir, camera["id"], age_hours=10, size=1_000_000)

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(
                    retention_days=None, max_bytes=100_000_000, min_free_bytes=None
                ),
                storage=AVAILABLE,
            )
        assert result.deleted == 0


class TestSafety:
    async def test_nothing_is_deleted_when_storage_is_unavailable(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        """Deleting against a storage we cannot trust would remove rows for
        footage that is merely unreachable."""
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        kept = await make_segment(recordings_dir, camera["id"], age_hours=24 * 100)

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(retention_days=1, max_bytes=None, min_free_bytes=None),
                storage=StorageStatus(path=recordings_dir, available=False, reason="not mounted"),
            )

        assert result.deleted == 0
        assert "skipped" in (result.reason or "")
        assert (recordings_dir / kept.path).exists()

    async def test_an_undeletable_file_keeps_its_row(
        self, admin_client: AsyncClient, recordings_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row is only dropped once the file is really gone, so a failure
        cannot leave the index claiming footage that still occupies the disk."""
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await make_segment(recordings_dir, camera["id"], age_hours=24 * 100)

        def refuse(self: Path, **_: object) -> None:
            raise PermissionError("in use")

        monkeypatch.setattr(Path, "unlink", refuse)

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(retention_days=1, max_bytes=None, min_free_bytes=None),
                storage=AVAILABLE,
            )

        assert result.freed_bytes == 0
        assert result.deleted == 0
        assert await segment_count() == 1

    async def test_size_trimming_does_not_retry_an_undeletable_row(
        self, admin_client: AsyncClient, recordings_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await make_segment(recordings_dir, camera["id"], age_hours=24, size=4_000_000)
        attempts = 0

        def refuse(self: Path, **_: object) -> None:
            nonlocal attempts
            attempts += 1
            raise PermissionError("in use")

        monkeypatch.setattr(Path, "unlink", refuse)

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(
                    retention_days=None, max_bytes=1_000_000, min_free_bytes=None
                ),
                storage=AVAILABLE,
            )

        assert result.deleted == 0
        assert result.freed_bytes == 0
        assert attempts == 1
        assert await segment_count() == 1

    async def test_an_already_missing_file_still_clears_its_row(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        segment = await make_segment(recordings_dir, camera["id"], age_hours=24 * 100)
        (recordings_dir / segment.path).unlink()

        async with session_scope() as session:
            result = await retention.sweep(
                session,
                recordings_dir,
                policy=RetentionPolicy(retention_days=1, max_bytes=None, min_free_bytes=None),
                storage=AVAILABLE,
            )

        assert result.deleted == 1
        assert await segment_count() == 0


class TestStorageApi:
    async def test_default_free_space_reserve_fits_the_installer_root_disk(
        self, admin_client: AsyncClient
    ) -> None:
        response = await admin_client.get("/api/v1/storage/settings")

        assert response.status_code == 200
        assert response.json()["min_free_bytes"] == 5 * 1024**3

    async def test_usage_reports_what_recordings_occupy(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await make_segment(recordings_dir, camera["id"], age_hours=1, size=4_000_000)
        await make_segment(
            recordings_dir, camera["id"], age_hours=2, size=4_000_000, protected=True
        )
        async with session_scope() as session:
            session.add(
                ExportJob(
                    camera_id=camera["id"],
                    start_at=datetime.now(UTC) - timedelta(minutes=2),
                    end_at=datetime.now(UTC) - timedelta(minutes=1),
                    status=ExportStatus.COMPLETE,
                    progress=1.0,
                    file_path="exports/saved.mp4",
                    size_bytes=2_000_000,
                    protected=True,
                )
            )
            await session.commit()

        response = await admin_client.get("/api/v1/storage")
        assert response.status_code == 200
        body = response.json()
        assert body["available"] is True
        assert body["recordings_bytes"] == 8_000_000
        assert body["segment_count"] == 2
        assert body["protected_count"] == 1
        assert body["protected_bytes"] == 4_000_000
        assert body["saved_clips_count"] == 1
        assert body["saved_clips_bytes"] == 2_000_000
        assert body["damaged_count"] == 0
        assert body["total_bytes"] > 0
        assert body["oldest_recording"] is not None
        assert body["recording_rate_bytes_per_hour"] is not None
        assert body["estimated_retention_days"] is not None
        assert body["estimated_days_remaining"] is not None
        assert body["configured_retention_days"] == 14
        assert body["camera_usage"] == [
            {
                "camera_id": camera["id"],
                "camera_name": "Porch",
                "bytes": 8_000_000,
                "segment_count": 2,
                "protected_bytes": 4_000_000,
                "oldest_recording": body["camera_usage"][0]["oldest_recording"],
                "newest_recording": body["camera_usage"][0]["newest_recording"],
                "recording_rate_bytes_per_hour": body["camera_usage"][0][
                    "recording_rate_bytes_per_hour"
                ],
            }
        ]

    async def test_settings_round_trip(self, admin_client: AsyncClient) -> None:
        payload = {"retention_days": 30, "min_free_bytes": 50 * 1024**3}
        preview = await admin_client.post("/api/v1/storage/settings/preview", json=payload)
        assert preview.status_code == 200
        response = await admin_client.patch(
            "/api/v1/storage/settings",
            json={
                **payload,
                "preview_token": preview.json()["preview_token"],
                "confirmation": preview.json()["required_confirmation"],
            },
        )
        assert response.status_code == 200
        assert response.json()["retention_days"] == 30

        async with session_scope() as session:
            stored = await settings_store.get_setting(
                session, settings_store.STORAGE_RETENTION_DAYS
            )
        assert stored == 30

    async def test_an_unusable_storage_path_is_refused(
        self, admin_client: AsyncClient, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "a-file-not-a-directory"
        blocker.write_text("x")

        response = await admin_client.patch("/api/v1/storage/settings", json={"path": str(blocker)})
        assert response.status_code == 422

    async def test_unknown_settings_are_refused(self, admin_client: AsyncClient) -> None:
        response = await admin_client.patch(
            "/api/v1/storage/settings", json={"delete_everything": True}
        )
        assert response.status_code == 422

    async def test_a_manual_sweep_reports_what_it_did(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await make_segment(recordings_dir, camera["id"], age_hours=24 * 100)
        preview = await admin_client.post(
            "/api/v1/storage/settings/preview", json={"retention_days": 7}
        )
        saved = await admin_client.patch(
            "/api/v1/storage/settings",
            json={
                "retention_days": 7,
                "preview_token": preview.json()["preview_token"],
                "confirmation": preview.json()["required_confirmation"],
            },
        )
        assert saved.status_code == 200

        response = await admin_client.post("/api/v1/storage/sweep")
        assert response.status_code == 200
        assert response.json()["deleted"] == 1
        assert await segment_count() == 0

    async def test_cleanup_previews_a_camera_and_requires_typed_confirmation(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await make_segment(recordings_dir, camera["id"], age_hours=48, size=3_000_000)
        await make_segment(
            recordings_dir,
            camera["id"],
            age_hours=48,
            size=5_000_000,
            protected=True,
        )

        preview = await admin_client.post(
            "/api/v1/storage/cleanup/preview",
            json={"camera_id": camera["id"], "include_protected": False},
        )
        assert preview.status_code == 200
        assert preview.json()["segment_count"] == 1
        assert preview.json()["bytes"] == 3_000_000
        assert preview.json()["required_confirmation"] == "DELETE SELECTED RECORDINGS"
        assert preview.json()["saved_clips_preserved"] is True

        refused = await admin_client.post(
            "/api/v1/storage/cleanup",
            json={"camera_id": camera["id"], "confirmation": "yes"},
        )
        assert refused.status_code == 422
        assert await segment_count() == 2

    async def test_cleanup_can_remove_all_segments_but_preserves_saved_exports(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        first = await make_segment(recordings_dir, camera["id"], age_hours=2, size=3_000_000)
        second = await make_segment(
            recordings_dir,
            camera["id"],
            age_hours=1,
            size=5_000_000,
            protected=True,
        )
        export_path = recordings_dir / ".exports" / "saved.mp4"
        export_path.parent.mkdir(parents=True)
        export_path.write_bytes(b"saved")
        async with session_scope() as session:
            session.add(
                ExportJob(
                    camera_id=camera["id"],
                    start_at=datetime.now(UTC) - timedelta(minutes=2),
                    end_at=datetime.now(UTC) - timedelta(minutes=1),
                    status=ExportStatus.COMPLETE,
                    progress=1.0,
                    file_path="saved.mp4",
                    size_bytes=5,
                    protected=True,
                )
            )

        response = await admin_client.post(
            "/api/v1/storage/cleanup",
            json={
                "include_protected": True,
                "confirmation": "DELETE ALL RECORDINGS",
            },
        )
        assert response.status_code == 202

        body: dict[str, object] = {}
        for _ in range(100):
            await asyncio.sleep(0.01)
            status_response = await admin_client.get("/api/v1/storage/cleanup")
            body = status_response.json()
            if body["phase"] in {"completed", "failed"}:
                break

        assert body["phase"] == "completed"
        assert body["deleted_segments"] == 2
        assert body["saved_clips_preserved"] is True
        assert await segment_count() == 0
        assert not (recordings_dir / first.path).exists()
        assert not (recordings_dir / second.path).exists()
        assert export_path.read_bytes() == b"saved"
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(ExportJob)) == 1

    async def test_storage_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/storage")).status_code == 401
        assert (await client.post("/api/v1/storage/sweep")).status_code == 401
        assert (await client.get("/api/v1/storage/cleanup")).status_code == 401

    async def test_timeline_range_reports_bounds_per_camera(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        camera = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await make_segment(recordings_dir, camera["id"], age_hours=10)
        await make_segment(recordings_dir, camera["id"], age_hours=1)

        body = (await admin_client.get("/api/v1/storage/timeline-range")).json()
        assert camera["id"] in body["cameras"]
        assert body["cameras"][camera["id"]]["from"] < body["cameras"][camera["id"]]["to"]
