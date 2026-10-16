"""Motion detection: the detector, the event aggregator and the events API."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.database.models import MotionEvent, RecordingSegment, SegmentStatus
from app.database.session import session_scope
from app.motion import service
from app.motion.aggregator import AggregatorConfig, MotionAggregator, Transition
from app.motion.detector import (
    ANALYSIS_HEIGHT,
    ANALYSIS_WIDTH,
    WARMUP_FRAMES,
    MotionDetector,
    MotionResult,
    MotionSettings,
    build_mask,
)
from app.motion.frames import FRAME_BYTES, build_frame_command

CAMERA_PAYLOAD = {
    "name": "Porch",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
    "sub_stream_url": "rtsp://192.168.50.50:554/stream2",
}


def blank_frame(value: int = 40) -> np.ndarray:
    """A featureless scene. Slight noise keeps MOG2 from an unrealistic model."""
    frame = np.full((ANALYSIS_HEIGHT, ANALYSIS_WIDTH), value, dtype=np.uint8)
    rng = np.random.default_rng(1234)
    noise = rng.integers(-2, 3, size=frame.shape, dtype=np.int16)
    return np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def frame_with_object(x: int, y: int = 150, size: int = 70, value: int = 220) -> np.ndarray:
    """A bright rectangle on the blank scene, standing in for a person."""
    frame = blank_frame()
    frame[y : y + size, x : x + size] = value
    return frame


def warm_up(detector: MotionDetector, frames: int = WARMUP_FRAMES + 5) -> None:
    for _ in range(frames):
        detector.process(blank_frame())


class TestDetectorWarmUp:
    def test_it_reports_nothing_until_the_background_is_learned(self) -> None:
        """Before MOG2 has a model it thinks an empty scene is unusual and would
        report the whole frame as motion."""
        detector = MotionDetector()
        results = [detector.process(blank_frame()) for _ in range(WARMUP_FRAMES)]

        assert all(result.warming_up for result in results)
        assert not any(result.has_motion for result in results)

    def test_it_becomes_warm_after_enough_frames(self) -> None:
        detector = MotionDetector()
        warm_up(detector)
        assert detector.is_warm is True

    def test_a_reset_discards_the_learned_background(self) -> None:
        """A reconnected stream may show a completely different scene."""
        detector = MotionDetector()
        warm_up(detector)
        detector.reset()
        assert detector.is_warm is False
        assert detector.process(blank_frame()).warming_up is True


class TestDetectorMotion:
    def test_a_static_scene_produces_no_motion(self) -> None:
        detector = MotionDetector()
        warm_up(detector)
        for _ in range(10):
            assert detector.process(blank_frame()).has_motion is False

    def test_an_object_appearing_is_detected(self) -> None:
        detector = MotionDetector()
        warm_up(detector)
        result = detector.process(frame_with_object(100))

        assert result.has_motion is True
        assert result.score > 0
        assert len(result.regions) >= 1

    def test_the_object_is_located_roughly_where_it_is(self) -> None:
        detector = MotionDetector()
        warm_up(detector)
        result = detector.process(frame_with_object(300, y=100, size=80))

        assert result.regions
        x, y, w, h = max(result.regions, key=lambda box: box[2] * box[3])
        assert 280 <= x <= 320
        assert 80 <= y <= 120
        assert w > 40 and h > 40

    def test_the_full_sequence_from_the_specification(self) -> None:
        """Empty, empty, object appears, object moves, object disappears."""
        detector = MotionDetector()
        warm_up(detector)

        sequence = [
            blank_frame(),
            blank_frame(),
            frame_with_object(100),
            frame_with_object(180),
            frame_with_object(260),
            blank_frame(),
        ]
        results = [detector.process(frame) for frame in sequence]

        assert results[0].has_motion is False
        assert results[1].has_motion is False
        assert results[2].has_motion is True, "the object appearing must register"
        assert results[3].has_motion is True, "the object moving must register"

    def test_a_larger_object_scores_higher(self) -> None:
        small = MotionDetector()
        warm_up(small)
        large = MotionDetector()
        warm_up(large)

        small_score = small.process(frame_with_object(100, size=40)).score
        large_score = large.process(frame_with_object(100, size=160)).score
        assert large_score > small_score

    def test_a_speck_below_the_minimum_area_is_ignored(self) -> None:
        """An insect on the lens or a compression artefact is not an event."""
        detector = MotionDetector(MotionSettings.from_sensitivity(50))
        warm_up(detector)
        assert detector.process(frame_with_object(100, size=4)).has_motion is False


class TestGlobalChangeRejection:
    def test_a_whole_frame_change_is_not_reported_as_motion(self) -> None:
        """A light switching on, or the infrared cut filter flipping, changes
        everything at once. Reporting those is the largest source of false
        events on an outdoor camera."""
        detector = MotionDetector()
        warm_up(detector)

        result = detector.process(blank_frame(value=230))
        assert result.is_global_change is True
        assert result.has_motion is False
        assert result.changed_fraction > 0.5

    def test_the_scene_is_relearned_immediately_after(self) -> None:
        """Without relearning, the new brightness would keep producing motion
        for the following minute."""
        detector = MotionDetector()
        warm_up(detector)
        detector.process(blank_frame(value=230))

        settled = [detector.process(blank_frame(value=230)) for _ in range(6)]
        assert not settled[-1].has_motion


class TestMotionZones:
    def test_movement_inside_an_ignored_zone_is_not_reported(self) -> None:
        detector = MotionDetector()
        # Ignore the whole left half, where the object will appear.
        detector.set_zones([[(0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)]])
        warm_up(detector)

        assert detector.process(frame_with_object(60, y=100, size=90)).has_motion is False

    def test_movement_outside_the_zone_is_still_reported(self) -> None:
        detector = MotionDetector()
        detector.set_zones([[(0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)]])
        warm_up(detector)

        assert detector.process(frame_with_object(420, y=100, size=90)).has_motion is True

    def test_a_mask_is_normalised_to_the_analysis_resolution(self) -> None:
        """Zones are stored 0-1 so a mask drawn on a 2K preview keeps working."""
        mask = build_mask([[(0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)]])
        assert mask is not None
        assert mask.shape == (ANALYSIS_HEIGHT, ANALYSIS_WIDTH)
        assert mask[10, 10] == 0
        assert mask[10, ANALYSIS_WIDTH - 10] == 255

    def test_degenerate_polygons_are_ignored(self) -> None:
        assert build_mask([]) is None
        assert build_mask([[(0.0, 0.0), (1.0, 1.0)]]) is None


class TestSensitivity:
    def test_higher_sensitivity_lowers_the_thresholds(self) -> None:
        low = MotionSettings.from_sensitivity(0)
        high = MotionSettings.from_sensitivity(100)
        assert high.threshold < low.threshold
        assert high.var_threshold < low.var_threshold
        assert high.min_area_fraction < low.min_area_fraction

    @pytest.mark.parametrize("value", [-50, 0, 50, 100, 150])
    def test_out_of_range_values_are_clamped(self, value: int) -> None:
        settings = MotionSettings.from_sensitivity(value)
        assert 0 <= settings.sensitivity <= 100
        assert settings.threshold > 0
        assert settings.min_area_fraction > 0

    def test_advanced_overrides_are_applied(self) -> None:
        """The advanced fields are hidden in the interface but must still work;
        MotionSettings is a slots dataclass, so this path cannot go through
        __dict__."""
        settings = MotionSettings.from_sensitivity(50, threshold=99, blur_kernel=9)
        assert settings.threshold == 99
        assert settings.blur_kernel == 9
        assert settings.sensitivity == 50


class TestAggregator:
    def _config(self, **overrides: float | int) -> AggregatorConfig:
        return AggregatorConfig(**{"end_grace_seconds": 3.0, **overrides})  # type: ignore[arg-type]

    def _motion(self, score: float = 0.05) -> MotionResult:
        return MotionResult(score=score, changed_fraction=score, regions=[(0, 0, 10, 10)])

    def _still(self) -> MotionResult:
        return MotionResult(score=0.0, changed_fraction=0.0)

    def test_one_noisy_frame_does_not_start_an_event(self) -> None:
        aggregator = MotionAggregator(config=self._config())
        now = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)

        outcome = aggregator.observe(self._motion(), now)
        assert outcome.transition is Transition.NONE
        assert aggregator.active_window is None

    def test_consecutive_frames_confirm_an_event(self) -> None:
        aggregator = MotionAggregator(config=self._config())
        now = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)

        aggregator.observe(self._motion(), now)
        aggregator.observe(self._motion(), now + timedelta(seconds=0.2))
        outcome = aggregator.observe(self._motion(), now + timedelta(seconds=0.4))

        assert outcome.transition is Transition.STARTED
        assert outcome.window is not None
        # Backdated to the first motion frame, not the confirming one.
        assert outcome.window.started_at == now

    def test_a_brief_gap_does_not_split_one_incident(self) -> None:
        """Someone passing behind a car must not become two events."""
        aggregator = MotionAggregator(config=self._config(end_grace_seconds=3.0))
        start = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)

        aggregator.observe(self._motion(), start)
        aggregator.observe(self._motion(), start + timedelta(seconds=0.2))
        aggregator.observe(self._motion(), start + timedelta(seconds=0.4))
        gap = aggregator.observe(self._still(), start + timedelta(seconds=1.2))
        assert gap.transition is Transition.CONTINUING

        resumed = aggregator.observe(self._motion(), start + timedelta(seconds=2.0))
        assert resumed.transition is Transition.CONTINUING
        assert aggregator.active_window is not None

    def test_the_event_ends_after_the_grace_period(self) -> None:
        aggregator = MotionAggregator(config=self._config(end_grace_seconds=3.0))
        start = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)

        aggregator.observe(self._motion(), start)
        aggregator.observe(self._motion(), start + timedelta(seconds=0.2))
        aggregator.observe(self._motion(), start + timedelta(seconds=0.4))
        outcome = aggregator.observe(self._still(), start + timedelta(seconds=4.0))

        assert outcome.transition is Transition.ENDED
        assert outcome.window is not None
        # Ends when motion last happened, not when the grace period expired.
        assert outcome.window.ended_at == start + timedelta(seconds=0.4)
        assert aggregator.active_window is None

    def test_the_peak_score_is_kept(self) -> None:
        aggregator = MotionAggregator(config=self._config())
        start = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)

        aggregator.observe(self._motion(0.02), start)
        aggregator.observe(self._motion(0.02), start + timedelta(seconds=0.2))
        peak = aggregator.observe(self._motion(0.4), start + timedelta(seconds=0.4))
        aggregator.observe(self._motion(0.1), start + timedelta(seconds=0.6))

        assert peak.is_peak is True
        assert aggregator.active_window is not None
        assert aggregator.active_window.peak_score == pytest.approx(0.4)

    def test_playback_bounds_include_the_roll(self) -> None:
        aggregator = MotionAggregator(
            config=self._config(pre_roll_seconds=5.0, post_roll_seconds=10.0)
        )
        start = datetime(2026, 8, 13, 15, 32, 20, tzinfo=UTC)

        aggregator.observe(self._motion(), start)
        aggregator.observe(self._motion(), start + timedelta(seconds=0.2))
        aggregator.observe(self._motion(), start + timedelta(seconds=0.4))
        outcome = aggregator.observe(self._still(), start + timedelta(seconds=30))

        assert outcome.window is not None
        pre, post = aggregator.playback_bounds(outcome.window)
        assert pre == start - timedelta(seconds=5)
        assert post == start + timedelta(seconds=0.4) + timedelta(seconds=10)

    def test_endless_movement_is_split_at_the_ceiling(self) -> None:
        """Rain on a lens must not produce one event that lasts all night."""
        aggregator = MotionAggregator(config=self._config(max_event_seconds=10))
        start = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)

        transitions = []
        for step in range(40):
            outcome = aggregator.observe(self._motion(), start + timedelta(seconds=step))
            transitions.append(outcome.transition)

        assert transitions.count(Transition.ENDED) >= 3

    def test_flush_closes_an_open_event(self) -> None:
        """A camera that goes offline mid-movement would otherwise leave an
        event the timeline draws as running for ever."""
        aggregator = MotionAggregator(config=self._config())
        start = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)

        aggregator.observe(self._motion(), start)
        aggregator.observe(self._motion(), start + timedelta(seconds=0.2))
        aggregator.observe(self._motion(), start + timedelta(seconds=0.4))
        outcome = aggregator.flush(start + timedelta(seconds=60))

        assert outcome.transition is Transition.ENDED
        assert outcome.window is not None
        assert outcome.window.ended_at is not None
        assert aggregator.active_window is None

    def test_flush_with_nothing_open_is_a_no_op(self) -> None:
        aggregator = MotionAggregator(config=self._config())
        assert aggregator.flush(datetime.now(UTC)).transition is Transition.NONE

    def test_a_global_change_never_starts_an_event(self) -> None:
        aggregator = MotionAggregator(config=self._config())
        now = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)
        flash = MotionResult(score=0.0, changed_fraction=0.9, is_global_change=True)

        for step in range(5):
            outcome = aggregator.observe(flash, now + timedelta(seconds=step))
            assert outcome.transition is Transition.NONE

    def test_unrelated_regions_do_not_combine_into_a_false_event(self) -> None:
        aggregator = MotionAggregator(config=self._config())
        now = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)

        positions = [(10, 10, 20, 20), (500, 280, 20, 20), (10, 10, 20, 20)]
        for step, region in enumerate(positions):
            result = MotionResult(score=0.05, changed_fraction=0.05, regions=[region])
            outcome = aggregator.observe(result, now + timedelta(seconds=step * 0.2))
            assert outcome.transition is Transition.NONE

        assert aggregator.active_window is None


class TestFrameCommand:
    def test_frames_are_greyscale_at_the_analysis_resolution(self) -> None:
        command = build_frame_command(
            "rtsp://127.0.0.1:8554/cam_x_sub", fps=5, ffmpeg_binary="ffmpeg"
        )
        filters = command[command.index("-vf") + 1]
        assert "fps=5" in filters
        assert f"scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT}" in filters
        assert command[command.index("-pix_fmt") + 1] == "gray"
        assert command[command.index("-f") + 1] == "rawvideo"

    def test_the_frame_rate_filter_runs_before_the_scale(self) -> None:
        """Otherwise every decoded frame is scaled and then thrown away."""
        filters = build_frame_command("rtsp://x/y", fps=5, ffmpeg_binary="ffmpeg")[
            build_frame_command("rtsp://x/y", fps=5, ffmpeg_binary="ffmpeg").index("-vf") + 1
        ]
        assert filters.index("fps=") < filters.index("scale=")

    def test_audio_is_not_decoded(self) -> None:
        assert "-an" in build_frame_command("rtsp://x/y", fps=5, ffmpeg_binary="ffmpeg")

    def test_the_frame_size_matches_the_analysis_resolution(self) -> None:
        assert FRAME_BYTES == ANALYSIS_WIDTH * ANALYSIS_HEIGHT


class TestZoneParsing:
    def test_valid_zones_round_trip(self) -> None:
        zones = [[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]]
        assert service.parse_zones(service.serialise_zones(zones)) == zones

    @pytest.mark.parametrize(
        "raw", [None, "", "not json", "{}", "[[]]", "[[[0.1, 0.1]]]", '["nonsense"]']
    )
    def test_malformed_zones_never_break_detection(self, raw: str | None) -> None:
        """The worst outcome of ignoring a bad value is more events, not none."""
        assert service.parse_zones(raw) == []


class TestEventPersistence:
    async def _camera(self, admin_client: AsyncClient) -> str:
        response = await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        return str(response.json()["id"])

    async def _event(self, camera_id: str, **overrides: object) -> MotionEvent:
        started = datetime(2026, 8, 13, 15, 32, 20, tzinfo=UTC)
        defaults: dict[str, object] = {
            "camera_id": camera_id,
            "started_at": started,
            "ended_at": started + timedelta(seconds=17),
            "duration_seconds": 17.0,
            "peak_score": 0.31,
            "pre_roll_start": started - timedelta(seconds=5),
            "post_roll_end": started + timedelta(seconds=27),
        }
        defaults.update(overrides)
        async with session_scope() as session:
            event = MotionEvent(**defaults)  # type: ignore[arg-type]
            session.add(event)
            await session.flush()
            return event

    async def test_events_are_listed_newest_first(self, admin_client: AsyncClient) -> None:
        camera_id = await self._camera(admin_client)
        await self._event(camera_id, started_at=datetime(2026, 8, 13, 10, 0, tzinfo=UTC))
        await self._event(camera_id, started_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC))

        body = (await admin_client.get("/api/v1/events")).json()
        assert body["total"] == 2
        assert body["events"][0]["started_at"].startswith("2026-08-13T12:00")

    async def test_the_response_never_exposes_a_disk_path(self, admin_client: AsyncClient) -> None:
        camera_id = await self._camera(admin_client)
        await self._event(
            camera_id,
            snapshot_path="cam/2026/08/13/abc.jpg",
            thumbnail_path="cam/2026/08/13/abc-thumb.jpg",
        )

        response = await admin_client.get("/api/v1/events")
        assert "snapshot_path" not in response.text
        assert ".jpg" not in response.text
        event = response.json()["events"][0]
        assert event["thumbnail_url"].endswith("/thumbnail")
        assert event["has_snapshot"] is True

    async def test_an_event_without_an_image_reports_no_urls(
        self, admin_client: AsyncClient
    ) -> None:
        camera_id = await self._camera(admin_client)
        await self._event(camera_id)
        event = (await admin_client.get("/api/v1/events")).json()["events"][0]
        assert event["has_snapshot"] is False
        assert event["thumbnail_url"] is None

    async def test_a_missing_image_file_is_a_404_not_a_crash(
        self, admin_client: AsyncClient
    ) -> None:
        camera_id = await self._camera(admin_client)
        event = await self._event(camera_id, thumbnail_path="cam/nope.jpg")
        response = await admin_client.get(f"/api/v1/events/{event.id}/thumbnail")
        assert response.status_code == 404

    async def test_an_image_path_escaping_its_root_is_refused(
        self, admin_client: AsyncClient
    ) -> None:
        camera_id = await self._camera(admin_client)
        event = await self._event(camera_id, thumbnail_path="../../../etc/passwd")
        response = await admin_client.get(f"/api/v1/events/{event.id}/thumbnail")
        assert response.status_code == 404

    async def test_an_event_can_be_protected(self, admin_client: AsyncClient) -> None:
        camera_id = await self._camera(admin_client)
        event = await self._event(camera_id)

        response = await admin_client.patch(f"/api/v1/events/{event.id}", json={"protected": True})
        assert response.status_code == 200
        assert response.json()["protected"] is True

    async def test_the_time_filter_keeps_an_event_still_running(
        self, admin_client: AsyncClient
    ) -> None:
        camera_id = await self._camera(admin_client)
        await self._event(camera_id, ended_at=None, duration_seconds=None)

        response = await admin_client.get(
            "/api/v1/events",
            params={"start": "2026-08-13T15:40:00Z", "end": "2026-08-13T15:50:00Z"},
        )
        assert response.json()["total"] == 1

    async def test_events_require_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/events")).status_code == 401

    async def test_event_cleanup_previews_exactly_and_requires_typed_confirmation(
        self, admin_client: AsyncClient, settings
    ) -> None:
        camera_id = await self._camera(admin_client)
        snapshot = settings.snapshots_dir / "camera" / "one.jpg"
        thumbnail = settings.thumbnails_dir / "camera" / "one.jpg"
        snapshot.parent.mkdir(parents=True)
        thumbnail.parent.mkdir(parents=True)
        snapshot.write_bytes(b"snapshot")
        thumbnail.write_bytes(b"thumb")
        await self._event(
            camera_id,
            duration_seconds=0.8,
            snapshot_path="camera/one.jpg",
            thumbnail_path="camera/one.jpg",
        )
        await self._event(camera_id, duration_seconds=12.0, protected=True)
        await self._event(camera_id, ended_at=None, duration_seconds=None)

        preview = await admin_client.post(
            "/api/v1/events/cleanup/preview",
            json={
                "camera_id": camera_id,
                "max_duration_seconds": 1,
                "include_protected": False,
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json() == {
            "event_count": 1,
            "protected_count": 0,
            "evidence_files": 2,
            "evidence_bytes": len(b"snapshot") + len(b"thumb"),
            "active_events_excluded": 1,
            "required_confirmation": "DELETE SELECTED EVENTS",
        }

        refused = await admin_client.post(
            "/api/v1/events/cleanup",
            json={"camera_id": camera_id, "confirmation": "yes"},
        )
        assert refused.status_code == 422
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(MotionEvent)) == 3

    async def test_event_cleanup_erases_history_but_preserves_recording_files(
        self, admin_client: AsyncClient, settings, recordings_dir
    ) -> None:
        camera_id = await self._camera(admin_client)
        for name, protected in (("normal", False), ("protected", True)):
            snapshot = settings.snapshots_dir / camera_id / f"{name}.jpg"
            thumbnail = settings.thumbnails_dir / camera_id / f"{name}.jpg"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            thumbnail.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_bytes(name.encode())
            thumbnail.write_bytes(name.encode())
            await self._event(
                camera_id,
                protected=protected,
                snapshot_path=f"{camera_id}/{name}.jpg",
                thumbnail_path=f"{camera_id}/{name}.jpg",
            )
        active = await self._event(camera_id, ended_at=None, duration_seconds=None)

        recording = recordings_dir / camera_id / "kept.mp4"
        recording.parent.mkdir(parents=True)
        recording.write_bytes(b"recorded video")
        async with session_scope() as session:
            session.add(
                RecordingSegment(
                    camera_id=camera_id,
                    path=f"{camera_id}/kept.mp4",
                    started_at=datetime(2026, 8, 13, 15, 0, tzinfo=UTC),
                    ended_at=datetime(2026, 8, 13, 16, 0, tzinfo=UTC),
                    duration_seconds=3600,
                    size_bytes=recording.stat().st_size,
                    has_motion=True,
                    motion_seconds=34,
                    status=SegmentStatus.COMPLETE,
                )
            )

        started = await admin_client.post(
            "/api/v1/events/cleanup",
            json={
                "include_protected": True,
                "confirmation": "DELETE ALL EVENTS",
            },
        )
        assert started.status_code == 202, started.text

        body: dict[str, object] = {}
        for _ in range(100):
            await asyncio.sleep(0.01)
            response = await admin_client.get("/api/v1/events/cleanup")
            body = response.json()
            if body["phase"] in {"completed", "failed"}:
                break

        assert body["phase"] == "completed", body
        assert body["deleted_events"] == 2
        assert body["recordings_preserved"] is True
        assert body["saved_exports_preserved"] is True
        assert recording.read_bytes() == b"recorded video"
        async with session_scope() as session:
            events = list((await session.execute(select(MotionEvent))).scalars())
            segments = list((await session.execute(select(RecordingSegment))).scalars())
        assert [event.id for event in events] == [active.id]
        assert len(segments) == 1
        assert segments[0].has_motion is True, "the active event still overlaps the footage"
        assert not any((settings.snapshots_dir / camera_id).glob("*.jpg"))
        assert not any((settings.thumbnails_dir / camera_id).glob("*.jpg"))


class TestSegmentAnnotation:
    async def test_an_event_marks_the_segments_it_overlaps(self, admin_client: AsyncClient) -> None:
        """This is what lets the timeline shade the periods with movement."""
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()["id"]
        base = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)

        async with session_scope() as session:
            for minute in range(3):
                session.add(
                    RecordingSegment(
                        camera_id=camera_id,
                        path=f"{camera_id}/seg-{minute}.mp4",
                        started_at=base + timedelta(minutes=minute),
                        ended_at=base + timedelta(minutes=minute + 1),
                        duration_seconds=60.0,
                        size_bytes=1000,
                        status=SegmentStatus.COMPLETE,
                    )
                )
            event = MotionEvent(
                camera_id=camera_id,
                started_at=base + timedelta(minutes=1, seconds=20),
                ended_at=base + timedelta(minutes=1, seconds=50),
                duration_seconds=30.0,
                pre_roll_start=base + timedelta(minutes=1, seconds=15),
            )
            session.add(event)
            await session.flush()
            touched = await service.mark_segments_with_motion(session, event)

        assert touched == 1
        async with session_scope() as session:
            rows = await session.execute(
                select(RecordingSegment).order_by(RecordingSegment.started_at)
            )
            segments = list(rows.scalars())
        assert [segment.has_motion for segment in segments] == [False, True, False]
        assert segments[1].motion_seconds == pytest.approx(30.0)

    async def test_an_event_spanning_two_segments_marks_both(
        self, admin_client: AsyncClient
    ) -> None:
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()["id"]
        base = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)

        async with session_scope() as session:
            for minute in range(2):
                session.add(
                    RecordingSegment(
                        camera_id=camera_id,
                        path=f"{camera_id}/span-{minute}.mp4",
                        started_at=base + timedelta(minutes=minute),
                        ended_at=base + timedelta(minutes=minute + 1),
                        duration_seconds=60.0,
                        size_bytes=1000,
                        status=SegmentStatus.COMPLETE,
                    )
                )
            event = MotionEvent(
                camera_id=camera_id,
                started_at=base + timedelta(seconds=50),
                ended_at=base + timedelta(minutes=1, seconds=10),
                duration_seconds=20.0,
                pre_roll_start=base + timedelta(seconds=45),
            )
            session.add(event)
            await session.flush()
            touched = await service.mark_segments_with_motion(session, event)

        assert touched == 2

    async def test_an_unfinished_event_marks_nothing_yet(self, admin_client: AsyncClient) -> None:
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()["id"]
        async with session_scope() as session:
            event = MotionEvent(
                camera_id=camera_id,
                started_at=datetime(2026, 8, 13, 15, 0, tzinfo=UTC),
                pre_roll_start=datetime(2026, 8, 13, 14, 59, 55, tzinfo=UTC),
            )
            session.add(event)
            await session.flush()
            assert await service.mark_segments_with_motion(session, event) == 0


class TestCrashRecovery:
    async def test_events_left_open_by_a_crash_are_closed(self, admin_client: AsyncClient) -> None:
        """An event with no end time would be drawn as still running, for ever."""
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()["id"]
        async with session_scope() as session:
            session.add(
                MotionEvent(
                    camera_id=camera_id,
                    started_at=datetime(2026, 8, 13, 15, 0, tzinfo=UTC),
                    pre_roll_start=datetime(2026, 8, 13, 14, 59, 55, tzinfo=UTC),
                )
            )

        async with session_scope() as session:
            closed = await service.close_orphaned_events(session)
        assert closed == 1

        async with session_scope() as session:
            event = (await session.execute(select(MotionEvent))).scalar_one()
        assert event.ended_at is not None
        assert event.duration_seconds == 0.0

    async def test_closing_orphans_is_idempotent(self, admin_client: AsyncClient) -> None:
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()["id"]
        async with session_scope() as session:
            session.add(
                MotionEvent(
                    camera_id=camera_id,
                    started_at=datetime(2026, 8, 13, 15, 0, tzinfo=UTC),
                    pre_roll_start=datetime(2026, 8, 13, 14, 59, 55, tzinfo=UTC),
                )
            )
        async with session_scope() as session:
            await service.close_orphaned_events(session)
        async with session_scope() as session:
            assert await service.close_orphaned_events(session) == 0


class TestMotionZonesApi:
    async def test_zones_round_trip_through_the_api(self, admin_client: AsyncClient) -> None:
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()["id"]
        zones = [[[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]]]

        response = await admin_client.put(
            f"/api/v1/cameras/{camera_id}/motion-zones", json={"zones": zones}
        )
        assert response.status_code == 200

        stored = await admin_client.get(f"/api/v1/cameras/{camera_id}/motion-zones")
        assert stored.json()["zones"] == [[[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]]]

    async def test_zones_can_be_cleared(self, admin_client: AsyncClient) -> None:
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()["id"]
        await admin_client.put(
            f"/api/v1/cameras/{camera_id}/motion-zones",
            json={"zones": [[[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]]},
        )
        response = await admin_client.put(
            f"/api/v1/cameras/{camera_id}/motion-zones", json={"zones": []}
        )
        assert response.json()["zones"] == []

    @pytest.mark.parametrize(
        "zones",
        [
            [[[0.1, 0.1], [0.5, 0.1]]],
            [[[1.5, 0.1], [0.5, 0.1], [0.5, 0.5]]],
            [[[-0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]],
        ],
    )
    async def test_invalid_zones_are_refused(
        self, admin_client: AsyncClient, zones: object
    ) -> None:
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()["id"]
        response = await admin_client.put(
            f"/api/v1/cameras/{camera_id}/motion-zones", json={"zones": zones}
        )
        assert response.status_code == 422

    async def test_zones_for_an_unknown_camera_are_404(self, admin_client: AsyncClient) -> None:
        assert (await admin_client.get("/api/v1/cameras/nope/motion-zones")).status_code == 404


class TestMotionHealth:
    async def test_motion_is_skipped_when_no_camera_wants_it(self, client: AsyncClient) -> None:
        body = (await client.get("/api/v1/system/health")).json()
        assert body["components"]["motion"]["state"] == "skipped"
        assert body["status"] == "healthy"

    async def test_motion_is_failed_when_expected_but_not_running(
        self, admin_client: AsyncClient
    ) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        body = (await admin_client.get("/api/v1/system/health")).json()
        assert body["components"]["motion"]["state"] == "failed"
        assert body["components"]["motion"]["data"]["expected"] == 1
