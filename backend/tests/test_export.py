"""Export: concat-demuxer clip production, job lifecycle and the API.

The real-FFmpeg suite follows the pattern in ``test_recording.py``: generate a
clip, segment it with the actual recorder arguments, index the result, then
exercise the code under test against real files instead of mocks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.cameras.probe import probe_stream
from app.config import Settings, get_settings
from app.database.models import ExportJob, ExportStatus, RecordingSegment, SegmentStatus
from app.database.session import session_scope
from app.recording import export, indexer, paths, recorder, timeline
from app.recording.export import ExportManager
from app.recording.recorder import RecorderInput, build_record_command
from app.storage.root import get_storage_root

FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for this test")

CAMERA_PAYLOAD = {
    "name": "Porch",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
}

DAY = datetime(2026, 8, 13, tzinfo=UTC)


async def _segment(camera_id: str, **overrides: object) -> RecordingSegment:
    defaults: dict[str, object] = {
        "camera_id": camera_id,
        "path": f"{camera_id}/seg-{uuid.uuid4()}.mp4",
        "started_at": DAY,
        "ended_at": DAY + timedelta(seconds=60),
        "duration_seconds": 60.0,
        "size_bytes": 6_000_000,
        "status": SegmentStatus.COMPLETE,
    }
    defaults.update(overrides)
    async with session_scope() as session:
        segment = RecordingSegment(**defaults)  # type: ignore[arg-type]
        session.add(segment)
        await session.flush()
        return segment


class TestConcatListWriting:
    def test_the_header_and_a_plain_file_line_are_written(self, tmp_path: Path) -> None:
        part = timeline.ExportPart(
            segment_id="a",
            started_at=DAY,
            ended_at=DAY + timedelta(seconds=5),
            trim_start_seconds=0.0,
            trim_end_seconds=0.0,
            duration_seconds=5.0,
            size_bytes=100,
        )
        list_path = tmp_path / "list.txt"
        missing = export._write_concat_list(list_path, [part], {"a": "cam/seg-a.mp4"}, tmp_path)

        content = list_path.read_text(encoding="utf-8")
        assert missing == []
        assert content.startswith("ffconcat version 1.0\n")
        assert "cam/seg-a.mp4" in content
        assert "inpoint" not in content
        assert "outpoint" not in content

    def test_trims_become_inpoint_and_outpoint_directives(self, tmp_path: Path) -> None:
        part = timeline.ExportPart(
            segment_id="a",
            started_at=DAY,
            ended_at=DAY + timedelta(seconds=5),
            trim_start_seconds=2.0,
            trim_end_seconds=3.0,
            duration_seconds=5.0,
            size_bytes=100,
        )
        list_path = tmp_path / "list.txt"
        export._write_concat_list(list_path, [part], {"a": "cam/seg-a.mp4"}, tmp_path)

        content = list_path.read_text(encoding="utf-8")
        assert "inpoint 2.000" in content
        # outpoint is trim_start + duration, i.e. the offset in the *original*
        # file where the used range ends -- not the file's own full length.
        assert "outpoint 7.000" in content

    def test_a_segment_gone_between_planning_and_writing_is_reported_missing(
        self, tmp_path: Path
    ) -> None:
        part = timeline.ExportPart(
            segment_id="ghost",
            started_at=DAY,
            ended_at=DAY + timedelta(seconds=5),
            trim_start_seconds=0.0,
            trim_end_seconds=0.0,
            duration_seconds=5.0,
            size_bytes=100,
        )
        list_path = tmp_path / "list.txt"
        missing = export._write_concat_list(list_path, [part], {}, tmp_path)

        assert missing == ["ghost"]
        assert list_path.read_text(encoding="utf-8").strip() == "ffconcat version 1.0"

    def test_an_embedded_quote_is_escaped_rather_than_left_to_break_the_line(self) -> None:
        assert export._concat_escape("a'b") == "a'\\''b"
        assert export._concat_escape("no quote here") == "no quote here"

    def test_paths_are_written_with_forward_slashes(self, tmp_path: Path) -> None:
        """Forward slashes are unambiguous inside the demuxer's quoted
        directive; a literal backslash risks being read as an escape."""
        part = timeline.ExportPart(
            segment_id="a",
            started_at=DAY,
            ended_at=DAY + timedelta(seconds=5),
            trim_start_seconds=0.0,
            trim_end_seconds=0.0,
            duration_seconds=5.0,
            size_bytes=100,
        )
        list_path = tmp_path / "list.txt"
        export._write_concat_list(list_path, [part], {"a": "cam/seg-a.mp4"}, tmp_path)
        content = list_path.read_text(encoding="utf-8")
        assert "\\" not in content.replace("'\\''", "")  # the one legitimate escape aside


class TestCreateJob:
    async def test_an_empty_range_is_rejected_with_a_clear_message(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            with pytest.raises(export.EmptyExportRangeError, match="no recorded footage"):
                await export.create_job(session, created["id"], DAY, DAY + timedelta(minutes=1))

    async def test_an_unknown_camera_raises_a_lookup_error(self, client: AsyncClient) -> None:
        # ``client`` is unused directly but its fixture chain is what creates
        # this test's own migrated database; without it session_scope() would
        # reuse whatever engine a previous test process left cached, pointed
        # at a tmp_path that no longer exists.
        del client
        async with session_scope() as session:
            with pytest.raises(LookupError):
                await export.create_job(session, "does-not-exist", DAY, DAY + timedelta(minutes=1))

    async def test_a_valid_range_is_queued(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await _segment(created["id"], started_at=DAY, ended_at=DAY + timedelta(seconds=60))

        async with session_scope() as session:
            job = await export.create_job(session, created["id"], DAY, DAY + timedelta(seconds=30))

        assert job.status is ExportStatus.QUEUED
        assert job.camera_id == created["id"]
        assert job.progress == 0.0

    async def test_a_backwards_range_is_rejected(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            with pytest.raises(timeline.TimelineRangeError):
                await export.create_job(session, created["id"], DAY, DAY - timedelta(seconds=1))


class TestExportPlanLimits:
    async def test_too_many_parts_is_rejected_rather_than_silently_truncated(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(timeline, "MAX_EXPORT_PARTS", 2)
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        for i in range(3):
            await _segment(
                created["id"],
                started_at=DAY + timedelta(seconds=i * 100),
                ended_at=DAY + timedelta(seconds=i * 100 + 60),
            )

        async with session_scope() as session:
            with pytest.raises(timeline.TimelineRangeError, match="too many segments"):
                await timeline.build_export_plan(
                    session, created["id"], DAY, DAY + timedelta(seconds=300)
                )


class TestReconcileStaleJobs:
    async def test_a_running_job_is_failed_with_an_actionable_reason(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            job = ExportJob(
                camera_id=created["id"],
                start_at=DAY,
                end_at=DAY + timedelta(minutes=1),
                status=ExportStatus.RUNNING,
            )
            session.add(job)
            await session.flush()
            job_id = job.id

        async with session_scope() as session:
            count = await export.reconcile_stale_jobs(session)
        assert count == 1

        async with session_scope() as session:
            reloaded = await session.get(ExportJob, job_id)
        assert reloaded is not None
        assert reloaded.status is ExportStatus.FAILED
        assert reloaded.error
        assert reloaded.finished_at is not None

    async def test_queued_and_complete_jobs_are_left_alone(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            session.add(
                ExportJob(
                    camera_id=created["id"],
                    start_at=DAY,
                    end_at=DAY + timedelta(minutes=1),
                    status=ExportStatus.QUEUED,
                )
            )
            session.add(
                ExportJob(
                    camera_id=created["id"],
                    start_at=DAY,
                    end_at=DAY + timedelta(minutes=1),
                    status=ExportStatus.COMPLETE,
                    progress=1.0,
                )
            )
            await session.flush()

        async with session_scope() as session:
            count = await export.reconcile_stale_jobs(session)
        assert count == 0


class TestExpireOldJobs:
    async def test_an_expired_completed_job_is_marked_expired_and_its_file_removed(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        exports_dir = export.exports_root(recordings_dir)
        exports_dir.mkdir(parents=True, exist_ok=True)
        (exports_dir / "old.mp4").write_bytes(b"stale export bytes")

        async with session_scope() as session:
            job = ExportJob(
                camera_id=created["id"],
                start_at=DAY,
                end_at=DAY + timedelta(minutes=1),
                status=ExportStatus.COMPLETE,
                progress=1.0,
                file_path="old.mp4",
                size_bytes=19,
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
            session.add(job)
            await session.flush()
            job_id = job.id

        async with session_scope() as session:
            count = await export.expire_old_jobs(session, recordings_dir)
        assert count == 1
        assert not (exports_dir / "old.mp4").exists()

        async with session_scope() as session:
            reloaded = await session.get(ExportJob, job_id)
        assert reloaded is not None
        assert reloaded.status is ExportStatus.EXPIRED
        assert reloaded.file_path is None
        assert reloaded.size_bytes is None

    async def test_a_job_not_yet_due_is_left_alone(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            session.add(
                ExportJob(
                    camera_id=created["id"],
                    start_at=DAY,
                    end_at=DAY + timedelta(minutes=1),
                    status=ExportStatus.COMPLETE,
                    progress=1.0,
                    file_path="fresh.mp4",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await session.flush()

        async with session_scope() as session:
            count = await export.expire_old_jobs(session, recordings_dir)
        assert count == 0

    async def test_a_protected_clip_is_never_expired_even_with_a_stale_date(
        self, admin_client: AsyncClient, recordings_dir: Path
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        exports_dir = export.exports_root(recordings_dir)
        exports_dir.mkdir(parents=True, exist_ok=True)
        saved = exports_dir / "saved.mp4"
        saved.write_bytes(b"permanent clip")

        async with session_scope() as session:
            job = ExportJob(
                camera_id=created["id"],
                start_at=DAY,
                end_at=DAY + timedelta(seconds=30),
                status=ExportStatus.COMPLETE,
                progress=1.0,
                file_path=saved.name,
                size_bytes=saved.stat().st_size,
                expires_at=datetime.now(UTC) - timedelta(days=30),
                protected=True,
            )
            session.add(job)
            await session.flush()
            job_id = job.id

        async with session_scope() as session:
            assert await export.expire_old_jobs(session, recordings_dir) == 0
            reloaded = await session.get(ExportJob, job_id)
            assert reloaded is not None
            assert reloaded.status is ExportStatus.COMPLETE
            assert reloaded.protected is True
        assert saved.is_file()


class TestManagerCancellationBookkeeping:
    """Pure state-machine checks that do not need a live process."""

    def test_cancel_only_succeeds_for_the_job_actually_marked_current(self) -> None:
        manager = ExportManager(get_settings())
        assert manager.request_cancel("nope") is False

        manager._current = export._RunningJob(job_id="abc", process=None)
        assert manager.request_cancel("abc") is True
        assert "abc" in manager._cancelled
        assert manager.request_cancel("someone-else") is False

    def test_is_running_reflects_the_current_job_only(self) -> None:
        manager = ExportManager(get_settings())
        assert manager.is_running("abc") is False
        manager._current = export._RunningJob(job_id="abc", process=None)
        assert manager.is_running("abc") is True
        assert manager.is_running("def") is False


class TestDeleteJob:
    async def test_deleting_a_queued_job_removes_the_row_without_touching_ffmpeg(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await _segment(created["id"], started_at=DAY, ended_at=DAY + timedelta(seconds=60))

        async with session_scope() as session:
            job = await export.create_job(session, created["id"], DAY, DAY + timedelta(seconds=30))
            job_id = job.id

        manager = ExportManager(get_settings())
        async with session_scope() as session:
            deleted = await manager.delete_job(session, job_id)
        assert deleted is True

        async with session_scope() as session:
            assert await session.get(ExportJob, job_id) is None

    async def test_deleting_an_unknown_job_reports_it_was_not_there(
        self, client: AsyncClient
    ) -> None:
        del client  # forces this test's own migrated database; see the note above
        manager = ExportManager(get_settings())
        async with session_scope() as session:
            assert await manager.delete_job(session, "does-not-exist") is False


# --- Real FFmpeg ----------------------------------------------------------


def _run_ffmpeg(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, capture_output=True, timeout=180, env=env)  # noqa: S603


@requires_ffmpeg
class TestRealExportPipeline:
    """Generates a clip, segments and indexes it for real, then runs an
    actual FFmpeg concat-demuxer export across the resulting boundary."""

    CAMERA_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    @pytest.fixture
    def recorded_segments(self, tmp_path: Path) -> Path:
        assert FFMPEG
        source = tmp_path / "source.mp4"
        _run_ffmpeg(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x180:rate=15:duration=7",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "15",
                str(source),
            ]
        )

        root = tmp_path / "recordings"
        pattern = recorder.prepare_output(root, self.CAMERA_ID)
        command = build_record_command(
            RecorderInput(url=str(source), via_go2rtc=False),
            pattern,
            ffmpeg_binary=FFMPEG,  # type: ignore[arg-type]
            segment_seconds=2,
        )
        for flag in ("-rtsp_transport", "-timeout"):
            index = command.index(flag)
            del command[index : index + 2]
        command.insert(command.index("-i"), "-re")

        _run_ffmpeg(command, env={**os.environ, **recorder.build_recorder_environment()})
        return root

    async def _index(
        self, admin_client: AsyncClient, recorded_segments: Path
    ) -> tuple[str, list[RecordingSegment]]:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        shutil.move(str(recorded_segments / self.CAMERA_ID), str(recorded_segments / camera_id))

        async with session_scope() as session:
            result = await indexer.index_camera(
                session, camera_id, recorded_segments, now=datetime.now(UTC) + timedelta(minutes=5)
            )
        assert result.indexed >= 2, "expected the clip to be split into at least two segments"

        async with session_scope() as session:
            rows = await session.execute(
                select(RecordingSegment)
                .where(RecordingSegment.camera_id == camera_id)
                .order_by(RecordingSegment.started_at.asc())
            )
            segments = list(rows.scalars())
        return camera_id, segments

    async def test_an_export_across_a_segment_boundary_produces_the_requested_span(
        self,
        settings: Settings,
        recorded_segments: Path,
        admin_client: AsyncClient,
    ) -> None:
        del settings
        camera_id, segments = await self._index(admin_client, recorded_segments)

        # Half a second into the first segment, and a third of a second short
        # of the last one's natural end -- both trims land mid-file, so both
        # the inpoint *and* the outpoint directives are actually exercised.
        start = segments[0].started_at + timedelta(seconds=0.5)
        assert segments[-1].ended_at is not None
        end = segments[-1].ended_at - timedelta(seconds=0.3)
        requested_seconds = (end - start).total_seconds()

        async with session_scope() as session:
            job = await export.create_job(session, camera_id, start, end)
            job_id = job.id

        manager = export.get_manager()
        processed = await manager.run_pending_once()
        assert processed is True

        async with session_scope() as session:
            finished = await session.get(ExportJob, job_id)
        assert finished is not None
        assert finished.status is ExportStatus.COMPLETE, finished.error
        assert finished.progress == 1.0
        assert finished.size_bytes is not None
        assert finished.size_bytes > 1024
        assert finished.file_path is not None
        assert finished.expires_at is not None

        async with session_scope() as session:
            root = await get_storage_root(session)
        output = export.resolve_output_file(root, finished)
        assert output.is_file()

        probed = await probe_stream(str(output))
        assert probed.codec == "h264"
        assert probed.duration_seconds is not None
        # A stream-copy export cannot start mid-GOP: the concat demuxer snaps
        # to the nearest keyframe at or before the requested start, so the
        # real output can be a little *longer* than asked for, never shorter
        # by more than rounding, and never wildly different (which is what a
        # "wrong file used" bug would look like).
        assert requested_seconds - 0.5 <= probed.duration_seconds <= requested_seconds + 2.0

    async def test_the_completed_export_downloads_and_can_then_be_deleted(
        self,
        settings: Settings,
        recorded_segments: Path,
        admin_client: AsyncClient,
    ) -> None:
        del settings
        camera_id, segments = await self._index(admin_client, recorded_segments)
        start = segments[0].started_at
        assert segments[-1].ended_at is not None
        end = segments[-1].ended_at

        create_response = await admin_client.post(
            "/api/v1/exports",
            json={"camera_id": camera_id, "start_at": start.isoformat(), "end_at": end.isoformat()},
        )
        assert create_response.status_code == 201
        assert create_response.json()["protected"] is True
        assert create_response.json()["expires_at"] is None
        job_id = create_response.json()["id"]

        await export.get_manager().run_pending_once()

        detail = await admin_client.get(f"/api/v1/exports/{job_id}")
        assert detail.json()["status"] == "complete"
        assert detail.json()["download_url"] == f"/api/v1/exports/{job_id}/download"
        assert "keyframe" in detail.json()["note"].lower()
        assert detail.json()["protected"] is True
        assert detail.json()["expires_at"] is None

        download = await admin_client.get(f"/api/v1/exports/{job_id}/download")
        assert download.status_code == 200
        assert download.headers["content-type"] == "video/mp4"
        assert download.headers["accept-ranges"] == "bytes"
        assert len(download.content) > 1024
        assert b"ftyp" in download.content[:64]

        delete_response = await admin_client.delete(f"/api/v1/exports/{job_id}")
        assert delete_response.status_code == 204

        gone = await admin_client.get(f"/api/v1/exports/{job_id}")
        assert gone.status_code == 404

    async def test_a_quote_in_a_real_segment_filename_does_not_break_the_concat_list(
        self, settings: Settings, recorded_segments: Path, admin_client: AsyncClient
    ) -> None:
        """Sentinel's own filenames never contain a quote, but the concat list
        writer is defensive regardless -- this proves the escaping actually
        satisfies FFmpeg's own parser, not just a regex in this suite.

        ``recorded_segments`` *is* the configured storage root (matching
        ``test_recording.py``'s own fixture), so the quoted file only needs to
        live under it with a path relative to that root -- no re-homing.
        """
        del settings
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]

        source_files = paths.iter_segment_files(recorded_segments, self.CAMERA_ID)
        assert source_files

        quoted_relative = "quoted/segment-it's-a-clip.mp4"
        quoted_file = recorded_segments / quoted_relative
        quoted_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_files[0], quoted_file)

        probed = await probe_stream(str(quoted_file))
        assert probed.duration_seconds is not None

        async with session_scope() as session:
            session.add(
                RecordingSegment(
                    camera_id=camera_id,
                    path=quoted_relative,
                    started_at=DAY,
                    ended_at=DAY + timedelta(seconds=probed.duration_seconds),
                    duration_seconds=probed.duration_seconds,
                    size_bytes=quoted_file.stat().st_size,
                    status=SegmentStatus.COMPLETE,
                )
            )
            await session.flush()

        async with session_scope() as session:
            job = await export.create_job(
                session, camera_id, DAY, DAY + timedelta(seconds=probed.duration_seconds)
            )
            job_id = job.id

        processed = await export.get_manager().run_pending_once()
        assert processed is True

        async with session_scope() as session:
            finished = await session.get(ExportJob, job_id)
        assert finished is not None
        assert finished.status is ExportStatus.COMPLETE, finished.error


class TestExportApi:
    async def test_create_requires_admin(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/exports",
            json={
                "camera_id": "x",
                "start_at": DAY.isoformat(),
                "end_at": (DAY + timedelta(seconds=60)).isoformat(),
            },
        )
        assert response.status_code == 401

    async def test_create_rejects_an_empty_range_with_a_422(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.post(
            "/api/v1/exports",
            json={
                "camera_id": created["id"],
                "start_at": DAY.isoformat(),
                "end_at": (DAY + timedelta(seconds=60)).isoformat(),
            },
        )
        assert response.status_code == 422
        assert "no recorded footage" in response.json()["detail"].lower()

    async def test_create_rejects_an_unknown_camera_with_a_404(
        self, admin_client: AsyncClient
    ) -> None:
        response = await admin_client.post(
            "/api/v1/exports",
            json={
                "camera_id": "does-not-exist",
                "start_at": DAY.isoformat(),
                "end_at": (DAY + timedelta(seconds=60)).isoformat(),
            },
        )
        assert response.status_code == 404

    async def test_create_rejects_end_before_start_at_the_schema_layer(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.post(
            "/api/v1/exports",
            json={
                "camera_id": created["id"],
                "start_at": DAY.isoformat(),
                "end_at": (DAY - timedelta(seconds=1)).isoformat(),
            },
        )
        assert response.status_code == 422

    async def test_list_filters_by_camera(self, admin_client: AsyncClient) -> None:
        first = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        second = (
            await admin_client.post(
                "/api/v1/cameras",
                json={**CAMERA_PAYLOAD, "name": "Carport", "main_stream_url": "rtsp://x/2"},
            )
        ).json()
        await _segment(first["id"], started_at=DAY, ended_at=DAY + timedelta(seconds=60))
        await _segment(second["id"], started_at=DAY, ended_at=DAY + timedelta(seconds=60))

        async with session_scope() as session:
            await export.create_job(session, first["id"], DAY, DAY + timedelta(seconds=30))
            await export.create_job(session, second["id"], DAY, DAY + timedelta(seconds=30))

        response = await admin_client.get("/api/v1/exports", params={"camera_id": first["id"]})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["exports"][0]["camera_id"] == first["id"]

    async def test_a_saved_clip_can_be_returned_to_expiry_and_saved_again(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        async with session_scope() as session:
            job = ExportJob(
                camera_id=created["id"],
                start_at=DAY,
                end_at=DAY + timedelta(seconds=30),
                status=ExportStatus.COMPLETE,
                progress=1.0,
                file_path="saved.mp4",
                protected=True,
            )
            session.add(job)
            await session.flush()
            job_id = job.id

        temporary = await admin_client.patch(f"/api/v1/exports/{job_id}", json={"protected": False})
        assert temporary.status_code == 200
        assert temporary.json()["protected"] is False
        assert temporary.json()["expires_at"] is not None

        saved = await admin_client.patch(f"/api/v1/exports/{job_id}", json={"protected": True})
        assert saved.status_code == 200
        assert saved.json()["protected"] is True
        assert saved.json()["expires_at"] is None

    async def test_updating_clip_protection_requires_admin(self, client: AsyncClient) -> None:
        response = await client.patch("/api/v1/exports/anything", json={"protected": True})
        assert response.status_code == 401

    async def test_getting_an_unknown_job_is_a_404(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/v1/exports/does-not-exist")
        assert response.status_code == 404

    async def test_downloading_a_job_that_is_not_complete_is_a_409(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await _segment(created["id"], started_at=DAY, ended_at=DAY + timedelta(seconds=60))
        async with session_scope() as session:
            job = await export.create_job(session, created["id"], DAY, DAY + timedelta(seconds=30))
            job_id = job.id

        response = await admin_client.get(f"/api/v1/exports/{job_id}/download")
        assert response.status_code == 409

    async def test_delete_requires_admin(self, client: AsyncClient) -> None:
        response = await client.delete("/api/v1/exports/anything")
        assert response.status_code == 401

    async def test_deleting_an_unknown_job_is_a_404(self, admin_client: AsyncClient) -> None:
        response = await admin_client.delete("/api/v1/exports/does-not-exist")
        assert response.status_code == 404

    async def test_deleting_a_queued_job_through_the_api(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await _segment(created["id"], started_at=DAY, ended_at=DAY + timedelta(seconds=60))
        async with session_scope() as session:
            job = await export.create_job(session, created["id"], DAY, DAY + timedelta(seconds=30))
            job_id = job.id

        response = await admin_client.delete(f"/api/v1/exports/{job_id}")
        assert response.status_code == 204
        assert (await admin_client.get(f"/api/v1/exports/{job_id}")).status_code == 404
