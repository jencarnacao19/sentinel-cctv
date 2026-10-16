"""Continuous playback: seeking to a segment and offset, and the manifest of
segments a player appends to cross a boundary without a freeze."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.database.models import RecordingSegment, SegmentStatus
from app.database.session import session_scope
from app.recording import playback, timeline

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


class TestResolvePlayback:
    async def test_an_instant_inside_a_segment_resolves_with_its_offset(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        segment = await _segment(camera_id)

        async with session_scope() as session:
            target = await timeline.resolve_playback(
                session, camera_id, DAY + timedelta(seconds=15)
            )

        assert target.found is True
        assert target.segment_id == segment.id
        assert target.offset_seconds == pytest.approx(15.0)
        assert target.gap_seconds is None

    async def test_an_instant_in_a_gap_reports_how_far_to_the_next_footage(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await _segment(
            camera_id,
            started_at=DAY + timedelta(seconds=100),
            ended_at=DAY + timedelta(seconds=160),
        )

        async with session_scope() as session:
            target = await timeline.resolve_playback(session, camera_id, DAY)

        assert target.found is False
        assert target.segment_id is None
        assert target.next_started_at == DAY + timedelta(seconds=100)
        assert target.gap_seconds == pytest.approx(100.0)

    async def test_an_instant_with_nothing_ahead_has_no_gap_estimate(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]

        async with session_scope() as session:
            target = await timeline.resolve_playback(session, camera_id, DAY)

        assert target.found is False
        assert target.gap_seconds is None

    async def test_adjacent_segments_are_reported_continuous(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await _segment(camera_id, started_at=DAY, ended_at=DAY + timedelta(seconds=60))
        await _segment(
            camera_id,
            started_at=DAY + timedelta(seconds=61),
            ended_at=DAY + timedelta(seconds=121),
        )

        async with session_scope() as session:
            target = await timeline.resolve_playback(
                session, camera_id, DAY + timedelta(seconds=10)
            )

        assert target.continuous is True

    async def test_a_real_gap_after_a_segment_is_not_continuous(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await _segment(camera_id, started_at=DAY, ended_at=DAY + timedelta(seconds=60))
        await _segment(
            camera_id,
            started_at=DAY + timedelta(seconds=300),
            ended_at=DAY + timedelta(seconds=360),
        )

        async with session_scope() as session:
            target = await timeline.resolve_playback(
                session, camera_id, DAY + timedelta(seconds=10)
            )

        assert target.continuous is False

    async def test_playbacks_resolve_seek_is_a_thin_wrapper_over_the_shared_lookup(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await _segment(camera_id)

        async with session_scope() as session:
            direct = await timeline.resolve_playback(session, camera_id, DAY + timedelta(seconds=5))
            wrapped = await playback.resolve_seek(session, camera_id, DAY + timedelta(seconds=5))

        assert direct == wrapped

    async def test_a_damaged_segment_is_never_offered_for_playback(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await _segment(camera_id, status=SegmentStatus.DAMAGED)

        async with session_scope() as session:
            target = await timeline.resolve_playback(
                session, camera_id, DAY + timedelta(seconds=10)
            )

        assert target.found is False


class TestBuildManifest:
    async def test_a_single_segment_becomes_one_manifest_item(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        segment = await _segment(camera_id)

        async with session_scope() as session:
            manifest = await playback.build_manifest(
                session, camera_id, DAY, DAY + timedelta(seconds=60)
            )

        assert len(manifest.items) == 1
        assert manifest.items[0].segment_id == segment.id
        assert manifest.items[0].offset_seconds == pytest.approx(0.0)
        assert manifest.gaps == []

    async def test_the_first_items_offset_reflects_where_the_range_begins(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await _segment(camera_id)

        async with session_scope() as session:
            manifest = await playback.build_manifest(
                session, camera_id, DAY + timedelta(seconds=20), DAY + timedelta(seconds=60)
            )

        assert len(manifest.items) == 1
        assert manifest.items[0].offset_seconds == pytest.approx(20.0)

    async def test_two_segments_across_a_boundary_both_appear_in_order(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        first = await _segment(camera_id, started_at=DAY, ended_at=DAY + timedelta(seconds=60))
        second = await _segment(
            camera_id,
            started_at=DAY + timedelta(seconds=60),
            ended_at=DAY + timedelta(seconds=120),
        )

        async with session_scope() as session:
            manifest = await playback.build_manifest(
                session, camera_id, DAY, DAY + timedelta(seconds=120)
            )

        assert [item.segment_id for item in manifest.items] == [first.id, second.id]
        assert manifest.total_seconds == pytest.approx(120.0)

    async def test_a_gap_between_segments_is_reported_not_bridged(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await _segment(camera_id, started_at=DAY, ended_at=DAY + timedelta(seconds=60))
        await _segment(
            camera_id,
            started_at=DAY + timedelta(seconds=120),
            ended_at=DAY + timedelta(seconds=180),
        )

        async with session_scope() as session:
            manifest = await playback.build_manifest(
                session, camera_id, DAY, DAY + timedelta(seconds=180)
            )

        assert len(manifest.items) == 2
        assert len(manifest.gaps) == 1
        assert manifest.gaps[0].start == DAY + timedelta(seconds=60)
        assert manifest.gaps[0].end == DAY + timedelta(seconds=120)

    async def test_a_damaged_segment_becomes_a_gap_rather_than_a_broken_append(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        await _segment(camera_id, started_at=DAY, ended_at=DAY + timedelta(seconds=60))
        await _segment(
            camera_id,
            started_at=DAY + timedelta(seconds=60),
            ended_at=DAY + timedelta(seconds=120),
            status=SegmentStatus.DAMAGED,
        )

        async with session_scope() as session:
            manifest = await playback.build_manifest(
                session, camera_id, DAY, DAY + timedelta(seconds=120)
            )

        assert len(manifest.items) == 1
        assert len(manifest.gaps) == 1

    async def test_an_empty_range_is_a_legitimate_answer_not_an_error(
        self, admin_client: AsyncClient
    ) -> None:
        """Unlike an export, scrubbing into a stretch with no footage yet is
        completely normal and must not raise."""
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]

        async with session_scope() as session:
            manifest = await playback.build_manifest(
                session, camera_id, DAY, DAY + timedelta(seconds=60)
            )

        assert manifest.items == []
        assert manifest.gaps == [timeline.TimeSpan(DAY, DAY + timedelta(seconds=60))]

    async def test_a_range_wider_than_the_manifest_cap_is_rejected(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]

        async with session_scope() as session:
            with pytest.raises(timeline.TimelineRangeError):
                await playback.build_manifest(
                    session,
                    camera_id,
                    DAY,
                    DAY + timedelta(seconds=playback.MAX_MANIFEST_SECONDS + 1),
                )


class TestPlaybackApi:
    async def test_manifest_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/v1/cameras/anything/playback/manifest",
            params={"start": "2026-08-13T00:00:00Z", "end": "2026-08-13T00:01:00Z"},
        )
        assert response.status_code == 401

    async def test_manifest_404s_for_an_unknown_camera(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get(
            "/api/v1/cameras/does-not-exist/playback/manifest",
            params={"start": "2026-08-13T00:00:00Z", "end": "2026-08-13T00:01:00Z"},
        )
        assert response.status_code == 404

    async def test_manifest_returns_download_urls_for_each_item(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        segment = await _segment(camera_id)

        response = await admin_client.get(
            f"/api/v1/cameras/{camera_id}/playback/manifest",
            params={"start": DAY.isoformat(), "end": (DAY + timedelta(seconds=60)).isoformat()},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["url"] == f"/api/v1/recordings/{segment.id}/download"

    async def test_seek_reports_a_found_target_with_its_url(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        segment = await _segment(camera_id)

        response = await admin_client.get(
            f"/api/v1/cameras/{camera_id}/playback/seek",
            params={"at": (DAY + timedelta(seconds=5)).isoformat()},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["found"] is True
        assert body["segment_id"] == segment.id
        assert body["url"] == f"/api/v1/recordings/{segment.id}/download"
        assert body["offset_seconds"] == pytest.approx(5.0)

    async def test_seek_into_a_gap_reports_not_found_with_no_url(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]

        response = await admin_client.get(
            f"/api/v1/cameras/{camera_id}/playback/seek",
            params={"at": DAY.isoformat()},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["found"] is False
        assert body["segment_id"] is None
        assert body["url"] is None
