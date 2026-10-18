"""Retention has to reclaim motion evidence, not only footage.

On a test installation this was the larger of the two problems: motion
snapshots and thumbnails were never pruned, so five days of a few cameras --
one of them high-resolution -- put 12 GiB across 21,821 files on a small system
disk that also holds the database. Because the sweep's accounting sums indexed segments
rather than walking the disk, none of it was visible: retention reported itself
satisfied while the disk filled to 98%.

Every test here asserts against real files on disk and real rows, because the
failure mode was precisely a sweep that believed it had done its job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.database.models import Camera, CameraKind, MotionEvent, RecordingMode
from app.database.session import session_scope
from app.storage import retention
from app.storage.retention import RetentionPolicy
from app.storage.root import StorageStatus
from app.system import settings_store

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

AVAILABLE = StorageStatus(
    path=Path("/recordings"),
    available=True,
    total_bytes=1_000_000_000,
    free_bytes=900_000_000,
    reason=None,
)


async def _camera(name: str = "Back door", retention_days: int | None = None) -> str:
    async with session_scope() as session:
        camera = Camera(
            name=name,
            kind=CameraKind.RTSP,
            enabled=False,
            host="192.168.50.185",
            main_stream_url="rtsp://192.168.50.185:554/stream1",
            recording_mode=RecordingMode.CONTINUOUS,
            retention_days=retention_days,
        )
        session.add(camera)
        await session.flush()
        return camera.id


async def _event(
    settings: Settings,
    camera_id: str,
    *,
    age_days: float,
    snapshot_bytes: int = 4096,
    thumbnail_bytes: int = 256,
    protected: bool = False,
    with_files: bool = True,
) -> str:
    """One motion event, with its two files actually written to disk."""
    started = NOW - timedelta(days=age_days)
    stamp = started.strftime("%Y%m%dT%H%M%S%f")
    relative = f"{camera_id}/{started:%Y}/{started:%m}/{started:%d}/{stamp}.jpg"

    if with_files:
        for base, size in (
            (settings.snapshots_dir, snapshot_bytes),
            (settings.thumbnails_dir, thumbnail_bytes),
        ):
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"j" * size)

    async with session_scope() as session:
        event = MotionEvent(
            camera_id=camera_id,
            started_at=started,
            ended_at=started + timedelta(seconds=8),
            duration_seconds=8.0,
            peak_score=0.7,
            pre_roll_start=started - timedelta(seconds=5),
            post_roll_end=started + timedelta(seconds=18),
            snapshot_path=relative if with_files else None,
            thumbnail_path=relative if with_files else None,
            protected=protected,
        )
        session.add(event)
        await session.flush()
        return event.id


async def _sweep(settings: Settings, *, retention_days: int | None = 14) -> retention.SweepResult:
    async with session_scope() as session:
        if retention_days is not None:
            await settings_store.set_setting(
                session, settings_store.STORAGE_RETENTION_DAYS, retention_days
            )
        policy = RetentionPolicy(retention_days=retention_days, max_bytes=None, min_free_bytes=None)
        return await retention.sweep(
            session,
            settings.recordings_dir,
            policy=policy,
            storage=AVAILABLE,
            now=NOW,
            settings=settings,
        )


async def _events_remaining() -> int:
    from sqlalchemy import func, select

    async with session_scope() as session:
        return int(await session.scalar(select(func.count()).select_from(MotionEvent)) or 0)


async def _event_paths(event_id: str) -> tuple[str | None, str | None]:
    async with session_scope() as session:
        event = await session.get(MotionEvent, event_id)
        assert event is not None
        return event.snapshot_path, event.thumbnail_path


class TestAgeBasedPruning:
    async def test_it_deletes_the_files_not_only_the_rows(
        self, app: object, settings: Settings
    ) -> None:
        camera_id = await _camera()
        await _event(settings, camera_id, age_days=30)

        snapshots = list(settings.snapshots_dir.rglob("*.jpg"))
        thumbnails = list(settings.thumbnails_dir.rglob("*.jpg"))
        assert len(snapshots) == 1
        assert len(thumbnails) == 1

        result = await _sweep(settings)

        assert result.events_deleted == 1
        assert result.event_bytes_freed == 4096 + 256
        assert await _events_remaining() == 0
        assert list(settings.snapshots_dir.rglob("*.jpg")) == []
        assert list(settings.thumbnails_dir.rglob("*.jpg")) == []

    async def test_it_keeps_evidence_inside_the_window(
        self, app: object, settings: Settings
    ) -> None:
        camera_id = await _camera()
        event_id = await _event(settings, camera_id, age_days=0.5)

        result = await _sweep(settings)

        assert result.events_deleted == 0
        assert result.evidence_references_cleared == 0
        assert await _events_remaining() == 1
        assert all(await _event_paths(event_id))
        assert len(list(settings.snapshots_dir.rglob("*.jpg"))) == 1

    async def test_it_sheds_images_after_24_hours_but_keeps_event_metadata(
        self, app: object, settings: Settings
    ) -> None:
        camera_id = await _camera()
        event_id = await _event(settings, camera_id, age_days=1.1)

        result = await _sweep(settings)

        assert result.events_deleted == 0, "the normal 14-day event policy has not expired"
        assert result.evidence_references_cleared == 2
        assert result.event_bytes_freed == 4096 + 256
        assert result.did_anything is True
        assert await _events_remaining() == 1
        assert await _event_paths(event_id) == (None, None)
        assert list(settings.snapshots_dir.rglob("*.jpg")) == []
        assert list(settings.thumbnails_dir.rglob("*.jpg")) == []

    async def test_it_honours_a_per_camera_retention_override(
        self, app: object, settings: Settings
    ) -> None:
        keeps_a_month = await _camera("Back door", retention_days=30)
        default = await _camera("Courtyard")
        await _event(settings, keeps_a_month, age_days=20)
        await _event(settings, default, age_days=20)

        result = await _sweep(settings, retention_days=14)

        assert result.events_deleted == 1, "only the camera on the default policy"
        assert await _events_remaining() == 1
        # The 30-day override keeps the timeline event, not 20 days of full-size JPEGs.
        async with session_scope() as session:
            remaining = (await session.execute(select(MotionEvent))).scalar_one()
            assert remaining.camera_id == keeps_a_month
            assert remaining.snapshot_path is None
            assert remaining.thumbnail_path is None

    async def test_it_never_deletes_a_protected_event(
        self, app: object, settings: Settings
    ) -> None:
        camera_id = await _camera()
        await _event(settings, camera_id, age_days=90, protected=True)

        result = await _sweep(settings)

        assert result.events_deleted == 0
        assert await _events_remaining() == 1
        assert len(list(settings.snapshots_dir.rglob("*.jpg"))) == 1

    async def test_without_event_retention_it_still_enforces_the_evidence_ceiling(
        self, app: object, settings: Settings
    ) -> None:
        camera_id = await _camera()
        event_id = await _event(settings, camera_id, age_days=365)

        result = await _sweep(settings, retention_days=None)

        assert result.events_deleted == 0
        assert result.evidence_references_cleared == 2
        assert await _events_remaining() == 1
        assert await _event_paths(event_id) == (None, None)

    async def test_a_cameras_own_period_applies_to_events_without_a_global_one(
        self, app: object, settings: Settings
    ) -> None:
        limited = await _camera("Limited", retention_days=3)
        unlimited = await _camera("Unlimited")
        await _event(settings, limited, age_days=5)
        await _event(settings, unlimited, age_days=5)

        result = await _sweep(settings, retention_days=None)

        assert result.events_deleted == 1
        assert await _events_remaining() == 1

    async def test_an_event_whose_files_are_already_gone_is_still_pruned(
        self, app: object, settings: Settings
    ) -> None:
        """The row is the leak once the files have gone."""
        camera_id = await _camera()
        await _event(settings, camera_id, age_days=30, with_files=False)

        result = await _sweep(settings)

        assert result.events_deleted == 1
        assert result.event_bytes_freed == 0
        assert await _events_remaining() == 0

    async def test_a_missing_file_does_not_stop_the_sweep(
        self, app: object, settings: Settings
    ) -> None:
        camera_id = await _camera()
        await _event(settings, camera_id, age_days=30)
        await _event(settings, camera_id, age_days=31)
        # One event's snapshot vanished underneath us.
        for path in settings.snapshots_dir.rglob("*.jpg"):
            path.unlink()
            break

        result = await _sweep(settings)

        assert result.events_deleted == 2
        assert await _events_remaining() == 0


class TestHonestReporting:
    async def test_motion_evidence_is_counted_separately_from_footage(
        self, app: object, settings: Settings
    ) -> None:
        """ "Deleted 40 things" must not hide that all forty were thumbnails."""
        camera_id = await _camera()
        for index in range(3):
            await _event(settings, camera_id, age_days=30 + index)

        result = await _sweep(settings)

        assert result.deleted == 0, "no segments existed to delete"
        assert result.events_deleted == 3
        assert result.did_anything is True, "a sweep that cleared evidence did something"

    async def test_a_sweep_with_nothing_to_do_says_so(
        self, app: object, settings: Settings
    ) -> None:
        await _camera()
        result = await _sweep(settings)
        assert result.did_anything is False
        assert result.events_deleted == 0

    async def test_the_api_reports_the_event_counts(
        self, admin_client: object, settings: Settings
    ) -> None:
        from httpx import AsyncClient

        assert isinstance(admin_client, AsyncClient)
        camera_id = await _camera()
        await _event(settings, camera_id, age_days=40)
        async with session_scope() as session:
            await settings_store.set_setting(session, settings_store.STORAGE_RETENTION_DAYS, 14)

        response = await admin_client.post("/api/v1/storage/sweep")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["events_deleted"] == 1
        assert body["event_bytes_freed"] > 0
        assert body["evidence_references_cleared"] == 2
        assert body["evidence_failures"] == 0


class TestBoundaries:
    async def test_the_pass_is_bounded(self) -> None:
        assert retention.MAX_EVENT_DELETIONS_PER_SWEEP > 0

    async def test_it_refuses_a_path_outside_its_own_directory(
        self, app: object, settings: Settings
    ) -> None:
        """A stored path must never be able to reach outside its tree."""
        camera_id = await _camera()
        outside = settings.data_dir / "escaped.jpg"
        outside.write_bytes(b"not a motion snapshot")

        async with session_scope() as session:
            session.add(
                MotionEvent(
                    camera_id=camera_id,
                    started_at=NOW - timedelta(days=30),
                    ended_at=NOW - timedelta(days=30),
                    duration_seconds=1.0,
                    peak_score=0.5,
                    pre_roll_start=NOW - timedelta(days=30),
                    post_roll_end=NOW - timedelta(days=30),
                    snapshot_path="../escaped.jpg",
                    thumbnail_path=None,
                    protected=False,
                )
            )

        result = await _sweep(settings)

        assert result.events_deleted == 0, "an unresolved managed path keeps the row retryable"
        assert result.evidence_failures == 1
        assert await _events_remaining() == 1
        async with session_scope() as session:
            event = (await session.execute(select(MotionEvent))).scalar_one()
            assert event.snapshot_path == "../escaped.jpg"
        assert outside.exists(), "a file outside the snapshots tree is left alone"

    async def test_an_unlink_failure_keeps_the_row_and_path_for_retry(
        self,
        app: object,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        camera_id = await _camera()
        event_id = await _event(settings, camera_id, age_days=30)
        snapshot = next(settings.snapshots_dir.rglob("*.jpg"))
        original_unlink = Path.unlink

        def fail_snapshot(path: Path, *args: object, **kwargs: object) -> None:
            if path == snapshot:
                raise PermissionError("simulated busy file")
            original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_snapshot)
        result = await _sweep(settings)

        assert result.events_deleted == 0
        assert result.evidence_failures == 1
        assert result.evidence_references_cleared == 1, "the independent thumbnail succeeded"
        snapshot_path, thumbnail_path = await _event_paths(event_id)
        assert snapshot_path is not None
        assert thumbnail_path is None
        assert snapshot.exists()

        monkeypatch.setattr(Path, "unlink", original_unlink)
        retry = await _sweep(settings)
        assert retry.events_deleted == 1
        assert await _events_remaining() == 0


@pytest.mark.parametrize("age_days", [14.5, 20, 100])
async def test_anything_past_the_window_goes(
    app: object, settings: Settings, age_days: float
) -> None:
    camera_id = await _camera()
    await _event(settings, camera_id, age_days=age_days)
    result = await _sweep(settings)
    assert result.events_deleted == 1


@pytest.mark.parametrize("available", [True, False])
async def test_active_old_motion_event_keeps_metadata_and_evidence(
    app: object,
    settings: Settings,
    available: bool,
) -> None:
    camera_id = await _camera()
    event_id = await _event(settings, camera_id, age_days=30)
    async with session_scope() as session:
        event = await session.get(MotionEvent, event_id)
        assert event
        event.ended_at = None
    async with session_scope() as session:
        result = await retention.sweep(
            session,
            settings.recordings_dir,
            settings=settings,
            now=NOW,
            storage=AVAILABLE
            if available
            else StorageStatus(path=Path("/recordings"), available=False),
            policy=RetentionPolicy(retention_days=1, max_bytes=None, min_free_bytes=None),
        )
    assert result.events_deleted == result.evidence_references_cleared == 0
    assert await _events_remaining() == 1
    assert len(list(settings.snapshots_dir.rglob("*.jpg"))) == 1
    assert len(list(settings.thumbnails_dir.rglob("*.jpg"))) == 1
