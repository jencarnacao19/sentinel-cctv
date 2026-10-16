"""Timeline: span arithmetic, coverage/gap/motion aggregation, and the API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.database.models import MotionEvent, RecordingSegment, SegmentStatus
from app.database.session import session_scope
from app.recording import timeline
from app.recording.timeline import GapReason, TimeSpan

CAMERA_PAYLOAD = {
    "name": "Entrada",
    "main_stream_url": "rtsp://192.168.1.50:554/stream1",
}

DAY = datetime(2026, 8, 13, tzinfo=UTC)


def _span(start_offset: float, end_offset: float) -> TimeSpan:
    return TimeSpan(DAY + timedelta(seconds=start_offset), DAY + timedelta(seconds=end_offset))


class TestSpanHelpers:
    def test_adjacent_spans_within_tolerance_merge(self) -> None:
        merged = timeline.merge_spans([_span(0, 60), _span(61, 120)], tolerance_seconds=2.0)
        assert merged == [_span(0, 120)]

    def test_spans_further_apart_than_tolerance_stay_separate(self) -> None:
        merged = timeline.merge_spans([_span(0, 60), _span(65, 120)], tolerance_seconds=2.0)
        assert merged == [_span(0, 60), _span(65, 120)]

    def test_overlapping_spans_merge_to_their_outer_bound(self) -> None:
        merged = timeline.merge_spans([_span(0, 60), _span(30, 90)], tolerance_seconds=0.0)
        assert merged == [_span(0, 90)]

    def test_zero_width_spans_are_dropped(self) -> None:
        assert timeline.merge_spans([_span(10, 10)], tolerance_seconds=0.0) == []

    def test_a_hole_is_punched_out_of_a_covering_span(self) -> None:
        result = timeline.subtract_spans([_span(0, 100)], [_span(40, 60)])
        assert result == [_span(0, 40), _span(60, 100)]

    def test_a_hole_touching_the_edge_trims_rather_than_splits(self) -> None:
        result = timeline.subtract_spans([_span(0, 100)], [_span(0, 20)])
        assert result == [_span(20, 100)]

    def test_no_holes_returns_the_spans_unchanged_but_drops_empties(self) -> None:
        assert timeline.subtract_spans([_span(0, 10), _span(5, 5)], []) == [_span(0, 10)]

    def test_clamp_trims_to_the_window(self) -> None:
        window_start, window_end = DAY + timedelta(seconds=20), DAY + timedelta(seconds=50)
        clamped = timeline.clamp_span(_span(0, 100), window_start, window_end)
        assert clamped == _span(20, 50)

    def test_clamp_drops_a_span_entirely_outside_the_window(self) -> None:
        window_start, window_end = DAY + timedelta(seconds=20), DAY + timedelta(seconds=50)
        clamped = timeline.clamp_span(_span(0, 10), window_start, window_end)
        assert clamped is None

    def test_complement_finds_the_gaps_between_coverage(self) -> None:
        gaps = timeline.complement_spans(
            [_span(10, 20), _span(30, 40)], DAY, DAY + timedelta(seconds=50)
        )
        assert gaps == [_span(0, 10), _span(20, 30), _span(40, 50)]

    def test_complement_of_full_coverage_is_empty(self) -> None:
        assert timeline.complement_spans([_span(0, 50)], DAY, DAY + timedelta(seconds=50)) == []

    def test_total_seconds_sums_durations(self) -> None:
        assert timeline.total_seconds([_span(0, 10), _span(20, 25)]) == 15.0


class TestCheckRange:
    def test_an_inverted_range_is_rejected(self) -> None:
        with pytest.raises(timeline.TimelineRangeError):
            timeline.check_range(DAY, DAY - timedelta(seconds=1), max_seconds=3600)

    def test_an_equal_range_is_rejected(self) -> None:
        with pytest.raises(timeline.TimelineRangeError):
            timeline.check_range(DAY, DAY, max_seconds=3600)

    def test_a_range_wider_than_the_cap_is_rejected(self) -> None:
        with pytest.raises(timeline.TimelineRangeError, match="too long"):
            timeline.check_range(DAY, DAY + timedelta(seconds=7200), max_seconds=3600)

    def test_a_naive_datetime_is_treated_as_utc_rather_than_raising(self) -> None:
        naive_start = datetime(2026, 8, 13, 0, 0, 0)
        naive_end = datetime(2026, 8, 13, 1, 0, 0)
        start, end = timeline.check_range(naive_start, naive_end, max_seconds=7200)
        assert start.tzinfo is UTC
        assert end.tzinfo is UTC


class TestBuildTimeline:
    async def _segment(self, camera_id: str, **overrides: object) -> RecordingSegment:
        defaults: dict[str, object] = {
            "camera_id": camera_id,
            "path": f"{camera_id}/seg-{overrides.get('started_at', DAY)!s}.mp4".replace(":", "-"),
            "started_at": DAY,
            "ended_at": DAY + timedelta(seconds=60),
            "duration_seconds": 60.0,
            "size_bytes": 1_000_000,
            "status": SegmentStatus.COMPLETE,
        }
        defaults.update(overrides)
        async with session_scope() as session:
            segment = RecordingSegment(**defaults)  # type: ignore[arg-type]
            session.add(segment)
            await session.flush()
            return segment

    async def _event(self, camera_id: str, **overrides: object) -> MotionEvent:
        defaults: dict[str, object] = {
            "camera_id": camera_id,
            "started_at": DAY + timedelta(seconds=10),
            "ended_at": DAY + timedelta(seconds=20),
            "duration_seconds": 10.0,
            "peak_score": 0.4,
            "pre_roll_start": DAY + timedelta(seconds=5),
            "post_roll_end": DAY + timedelta(seconds=25),
        }
        defaults.update(overrides)
        async with session_scope() as session:
            event = MotionEvent(**defaults)  # type: ignore[arg-type]
            session.add(event)
            await session.flush()
            return event

    async def test_two_adjacent_segments_report_as_one_covered_run(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await self._segment(camera_id, started_at=DAY, ended_at=DAY + timedelta(seconds=60))
        await self._segment(
            camera_id,
            started_at=DAY + timedelta(seconds=61),
            ended_at=DAY + timedelta(seconds=121),
        )

        async with session_scope() as session:
            result = await timeline.build_timeline(
                session, camera_id, DAY - timedelta(minutes=1), DAY + timedelta(minutes=5)
            )

        assert len(result.coverage) == 1
        assert result.coverage[0].start == DAY
        assert result.coverage[0].end == DAY + timedelta(seconds=121)
        assert result.recorded_seconds == pytest.approx(121.0)
        assert result.segment_count == 2

    async def test_a_damaged_segment_is_a_visible_scar_not_silent_coverage(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await self._segment(camera_id, started_at=DAY, ended_at=DAY + timedelta(seconds=60))
        await self._segment(
            camera_id,
            started_at=DAY + timedelta(seconds=60),
            ended_at=DAY + timedelta(seconds=120),
            status=SegmentStatus.DAMAGED,
        )
        await self._segment(
            camera_id,
            started_at=DAY + timedelta(seconds=120),
            ended_at=DAY + timedelta(seconds=180),
        )

        async with session_scope() as session:
            result = await timeline.build_timeline(
                session, camera_id, DAY, DAY + timedelta(seconds=180)
            )

        # The damaged stretch never gets painted over as coverage, even though
        # playable footage sits on both sides of it. The window exactly covers
        # the three segments, so the damaged middle is the only gap possible.
        assert len(result.coverage) == 2
        assert len(result.gaps) == 1
        assert result.gaps[0].reason is GapReason.DAMAGED
        assert result.gaps[0].start == DAY + timedelta(seconds=60)
        assert result.damaged_seconds == pytest.approx(60.0)

    async def test_a_missing_segment_leaves_an_ordinary_gap(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await self._segment(camera_id, status=SegmentStatus.MISSING)

        async with session_scope() as session:
            result = await timeline.build_timeline(
                session, camera_id, DAY - timedelta(minutes=1), DAY + timedelta(minutes=5)
            )

        assert result.coverage == []
        assert result.segment_count == 0
        assert len(result.gaps) == 1
        assert result.gaps[0].reason is GapReason.NOT_RECORDING

    async def test_a_recent_gap_is_blamed_on_storage_only_while_it_is_reported_unavailable(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        now = DAY + timedelta(minutes=5)
        await self._segment(camera_id, started_at=DAY, ended_at=DAY + timedelta(seconds=60))

        async with session_scope() as session:
            unavailable = await timeline.build_timeline(
                session, camera_id, DAY, now, now=now, storage_unavailable=True
            )
            available = await timeline.build_timeline(
                session, camera_id, DAY, now, now=now, storage_unavailable=False
            )

        after_footage = DAY + timedelta(seconds=60)
        trailing_unavailable = next(g for g in unavailable.gaps if g.start >= after_footage)
        trailing_available = next(g for g in available.gaps if g.start >= after_footage)
        assert trailing_unavailable.reason is GapReason.STORAGE_UNAVAILABLE
        assert trailing_available.reason is GapReason.NOT_RECORDING

    async def test_an_old_gap_is_not_blamed_on_a_currently_unavailable_mount(
        self, admin_client: AsyncClient
    ) -> None:
        """Sentinel keeps no history of *when* storage came and went, so a gap
        that ended long ago cannot honestly be pinned on an outage that is only
        known to be happening right now."""
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        now = DAY + timedelta(hours=3)
        await self._segment(camera_id, started_at=DAY, ended_at=DAY + timedelta(seconds=60))

        async with session_scope() as session:
            result = await timeline.build_timeline(
                session, camera_id, DAY, DAY + timedelta(hours=1), now=now, storage_unavailable=True
            )

        trailing = next(g for g in result.gaps if g.start >= DAY + timedelta(seconds=60))
        assert trailing.reason is GapReason.NOT_RECORDING

    async def test_motion_is_reported_as_a_band_with_its_peak_score(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await self._event(camera_id, peak_score=0.75)

        async with session_scope() as session:
            result = await timeline.build_timeline(
                session, camera_id, DAY, DAY + timedelta(minutes=1)
            )

        assert len(result.motion) == 1
        assert result.motion[0].peak_score == 0.75
        assert result.motion_event_count == 1
        assert result.motion_seconds == pytest.approx(10.0)

    async def test_motion_spanning_the_window_edge_is_clamped_not_dropped(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await self._event(
            camera_id,
            started_at=DAY - timedelta(seconds=5),
            ended_at=DAY + timedelta(seconds=5),
        )

        async with session_scope() as session:
            result = await timeline.build_timeline(
                session, camera_id, DAY, DAY + timedelta(minutes=1)
            )

        assert len(result.motion) == 1
        assert result.motion[0].start == DAY  # clamped, not the true (earlier) start

    async def test_two_close_events_merge_into_one_band_keeping_the_higher_peak(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await self._event(
            camera_id,
            started_at=DAY,
            ended_at=DAY + timedelta(seconds=10),
            peak_score=0.3,
        )
        await self._event(
            camera_id,
            started_at=DAY + timedelta(seconds=11),
            ended_at=DAY + timedelta(seconds=20),
            peak_score=0.9,
        )

        async with session_scope() as session:
            result = await timeline.build_timeline(
                session, camera_id, DAY, DAY + timedelta(minutes=1)
            )

        assert len(result.motion) == 1
        assert result.motion[0].peak_score == 0.9
        assert result.motion[0].event_count == 2

    async def test_reading_more_segments_than_the_limit_sets_truncated(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        for i in range(5):
            await self._segment(
                camera_id,
                started_at=DAY + timedelta(seconds=i * 100),
                ended_at=DAY + timedelta(seconds=i * 100 + 60),
            )

        async with session_scope() as session:
            segments, truncated = await timeline._load_segments(
                session, camera_id, DAY, DAY + timedelta(hours=1), now=DAY, limit=3
            )
        assert truncated is True
        assert len(segments) == 3

    async def test_a_wide_window_coarsens_rather_than_returning_every_span(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        # Twenty separated one-second segments -- far more than a max_buckets
        # of 4 can express individually.
        for i in range(20):
            await self._segment(
                camera_id,
                started_at=DAY + timedelta(seconds=i * 120),
                ended_at=DAY + timedelta(seconds=i * 120 + 1),
            )

        async with session_scope() as session:
            result = await timeline.build_timeline(
                session, camera_id, DAY, DAY + timedelta(seconds=20 * 120), max_buckets=4
            )

        assert len(result.coverage) <= 4
        assert result.bucket_seconds > timeline.SEGMENT_JOIN_TOLERANCE_SECONDS
        # Coarsening only changes what is drawn -- the accounted total footage
        # must stay exact.
        assert result.recorded_seconds == pytest.approx(20.0)

    async def test_range_validation_is_enforced_by_the_public_function(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        async with session_scope() as session:
            with pytest.raises(timeline.TimelineRangeError):
                await timeline.build_timeline(session, camera_id, DAY, DAY - timedelta(seconds=1))


class TestTimelineApi:
    async def test_it_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/v1/cameras/anything/timeline",
            params={"start": "2026-08-13T00:00:00Z", "end": "2026-08-13T01:00:00Z"},
        )
        assert response.status_code == 401

    async def test_an_unknown_camera_is_a_404(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get(
            "/api/v1/cameras/does-not-exist/timeline",
            params={"start": "2026-08-13T00:00:00Z", "end": "2026-08-13T01:00:00Z"},
        )
        assert response.status_code == 404

    async def test_an_inverted_range_is_a_422_not_a_500(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.get(
            f"/api/v1/cameras/{created['id']}/timeline",
            params={"start": "2026-08-13T01:00:00Z", "end": "2026-08-13T00:00:00Z"},
        )
        assert response.status_code == 422

    async def test_missing_query_parameters_are_a_422(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        response = await admin_client.get(f"/api/v1/cameras/{created['id']}/timeline")
        assert response.status_code == 422

    async def test_a_normal_request_reports_coverage_and_gaps(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        async with session_scope() as session:
            session.add(
                RecordingSegment(
                    camera_id=camera_id,
                    path=f"{camera_id}/seg.mp4",
                    started_at=DAY,
                    ended_at=DAY + timedelta(seconds=60),
                    duration_seconds=60.0,
                    size_bytes=1000,
                    status=SegmentStatus.COMPLETE,
                )
            )
            await session.flush()

        response = await admin_client.get(
            f"/api/v1/cameras/{camera_id}/timeline",
            params={
                "start": DAY.isoformat(),
                "end": (DAY + timedelta(minutes=5)).isoformat(),
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["camera_id"] == camera_id
        assert len(body["coverage"]) == 1
        assert body["coverage"][0]["duration_seconds"] == pytest.approx(60.0)
        assert any(gap["reason"] == "not_recording" for gap in body["gaps"])
        assert body["truncated"] is False
