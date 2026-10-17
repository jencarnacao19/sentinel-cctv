"""Named motion zones: detection zones, per-zone sensitivity and zone events.

Ignore zones are covered in ``test_motion.py``; this file covers what a
detection zone adds on top: only its area counts, it can be more or less
sensitive than the camera, and it starts and stops on its own, which is what
gives Home Assistant one motion sensor per zone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.database.models import Camera, MotionEvent
from app.database.session import session_scope
from app.motion import service
from app.motion.aggregator import AggregatorConfig
from app.motion.detector import MotionDetector, MotionResult, ZoneResult
from app.motion.worker import MotionWorker
from app.motion.zones import MotionZone, ZoneKind, serialise_zones
from tests.test_motion import (
    CAMERA_PAYLOAD,
    LEFT_HALF,
    RIGHT_HALF,
    detect_zone,
    frame_with_object,
    ignore_zone,
    warm_up,
)


class TestDetectionZones:
    def test_movement_outside_every_detection_zone_is_not_reported(self) -> None:
        detector = MotionDetector()
        detector.set_zones([detect_zone(RIGHT_HALF)])
        warm_up(detector)

        result = detector.process(frame_with_object(60, size=90))
        assert result.has_motion is False
        assert result.zones == {}

    def test_movement_inside_a_detection_zone_is_reported_for_that_zone(self) -> None:
        detector = MotionDetector()
        detector.set_zones([detect_zone(LEFT_HALF, "drive"), detect_zone(RIGHT_HALF, "gate")])
        warm_up(detector)

        result = detector.process(frame_with_object(420, size=90))
        assert result.has_motion is True
        assert set(result.zones) == {"gate"}
        assert result.score == result.zones["gate"].score

    def test_an_ignore_zone_still_cuts_into_a_detection_zone(self) -> None:
        detector = MotionDetector()
        detector.set_zones([detect_zone(RIGHT_HALF), ignore_zone(RIGHT_HALF)])
        warm_up(detector)

        assert detector.process(frame_with_object(420, size=90)).has_motion is False

    def test_a_disabled_zone_has_no_effect(self) -> None:
        detector = MotionDetector()
        disabled = MotionZone(
            id="gate", name="Gate", kind=ZoneKind.DETECT, points=RIGHT_HALF, enabled=False
        )
        detector.set_zones([disabled])
        warm_up(detector)

        # With no enabled detection zone the whole frame counts again.
        result = detector.process(frame_with_object(60, size=90))
        assert result.has_motion is True
        assert result.zones == {}


class TestPerZoneSensitivity:
    """A small object: about 0.07% of the frame, below the default threshold."""

    SMALL = 14

    def _detects_small_object(self, sensitivity: int | None) -> bool:
        detector = MotionDetector()
        detector.set_zones([detect_zone(RIGHT_HALF, sensitivity=sensitivity)])
        warm_up(detector)
        return detector.process(frame_with_object(420, size=self.SMALL)).has_motion

    def test_a_sensitive_zone_catches_what_the_camera_default_misses(self) -> None:
        assert self._detects_small_object(None) is False
        assert self._detects_small_object(100) is True

    def test_a_dull_zone_needs_a_bigger_movement(self) -> None:
        detector = MotionDetector()
        detector.set_zones([detect_zone(RIGHT_HALF, sensitivity=0)])
        warm_up(detector)
        assert detector.process(frame_with_object(420, size=30)).has_motion is False
        assert detector.process(frame_with_object(420, size=90)).has_motion is True

    def test_one_zones_sensitivity_does_not_leak_into_another(self) -> None:
        detector = MotionDetector()
        detector.set_zones(
            [
                detect_zone(LEFT_HALF, "drive", sensitivity=0),
                detect_zone(RIGHT_HALF, "gate", sensitivity=100),
            ]
        )
        warm_up(detector)
        assert set(detector.process(frame_with_object(60, size=self.SMALL)).zones) == set()
        warm_up(detector)
        assert set(detector.process(frame_with_object(420, size=self.SMALL)).zones) == {"gate"}


def _moving(zone_id: str, score: float = 0.05) -> MotionResult:
    region = (10, 10, 40, 40)
    return MotionResult(
        score=score,
        changed_fraction=score,
        regions=[region],
        zones={zone_id: ZoneResult(score=score, regions=[region])},
    )


STILL = MotionResult(score=0.0, changed_fraction=0.0)


async def _camera_with_zones(admin_client: AsyncClient, zones: list[MotionZone]) -> Camera:
    camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()["id"]
    async with session_scope() as session:
        camera = await session.get(Camera, camera_id)
        assert camera is not None
        camera.motion_zones = serialise_zones(zones)
    async with session_scope() as session:
        stored = await session.get(Camera, camera_id)
        assert stored is not None
        session.expunge(stored)
        return stored


class TestZoneEvents:
    async def test_each_zone_starts_and_ends_on_its_own(
        self,
        admin_client: AsyncClient,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        published: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            service, "publish", lambda name, data: published.append((name, dict(data)))
        )
        camera = await _camera_with_zones(
            admin_client, [detect_zone(LEFT_HALF, "drive"), detect_zone(RIGHT_HALF, "gate")]
        )
        worker = MotionWorker(camera, settings, AggregatorConfig(end_grace_seconds=8.0))

        async def no_snapshot(*args: object, **kwargs: object) -> None:
            return None

        monkeypatch.setattr(worker, "_maybe_snapshot", no_snapshot)

        start = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
        for frame in range(3):
            await worker._handle(_moving("gate"), start + timedelta(seconds=frame * 0.2))
        assert worker.active_zone_ids == {"gate"}

        names = [name for name, _ in published]
        assert "motion.started" in names
        zone_started = [data for name, data in published if name == "motion.zone_started"]
        assert [(d["zone_id"], d["zone"]) for d in zone_started] == [("gate", "gate")]
        assert zone_started[0]["event_id"] is not None

        # Long enough after the last movement for the grace period to pass.
        await worker._handle(STILL, start + timedelta(seconds=30))
        assert worker.active_zone_ids == set()
        assert [d["zone_id"] for n, d in published if n == "motion.zone_ended"] == ["gate"]

        async with session_scope() as session:
            event = await session.get(MotionEvent, zone_started[0]["event_id"])
            assert event is not None
            assert json.loads(event.zones or "[]") == [{"id": "gate", "name": "gate"}]

    async def test_stopping_the_worker_ends_open_zones(
        self,
        admin_client: AsyncClient,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A sensor must not stay 'on' in Home Assistant after a disconnect."""
        published: list[str] = []
        monkeypatch.setattr(service, "publish", lambda name, data: published.append(name))
        camera = await _camera_with_zones(admin_client, [detect_zone(RIGHT_HALF, "gate")])
        worker = MotionWorker(camera, settings, AggregatorConfig())

        async def no_snapshot(*args: object, **kwargs: object) -> None:
            return None

        monkeypatch.setattr(worker, "_maybe_snapshot", no_snapshot)

        start = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
        for frame in range(3):
            await worker._handle(_moving("gate"), start + timedelta(seconds=frame * 0.2))
        await worker.stop()

        assert published.count("motion.zone_started") == 1
        assert published.count("motion.zone_ended") == 1
        assert worker.active_zone_ids == set()


class TestZonesInTheApi:
    async def test_home_assistant_sees_each_detection_zone(self, admin_client: AsyncClient) -> None:
        camera = await _camera_with_zones(
            admin_client,
            [
                detect_zone(LEFT_HALF, "drive"),
                ignore_zone(RIGHT_HALF, "tree"),
                MotionZone(
                    id="off", name="Off", kind=ZoneKind.DETECT, points=RIGHT_HALF, enabled=False
                ),
            ],
        )
        handshake = (await admin_client.get("/api/v1/integrations/home-assistant")).json()
        entry = next(c for c in handshake["cameras"] if c["id"] == camera.id)
        # Ignore zones and disabled zones get no sensor.
        assert entry["motion_zones"] == [{"id": "drive", "name": "drive", "active": False}]

    async def test_events_say_which_zones_saw_the_movement(self, admin_client: AsyncClient) -> None:
        camera = await _camera_with_zones(admin_client, [detect_zone(RIGHT_HALF, "gate")])
        started = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
        async with session_scope() as session:
            session.add(
                MotionEvent(
                    camera_id=camera.id,
                    started_at=started,
                    pre_roll_start=started - timedelta(seconds=5),
                    zones=json.dumps([{"id": "gate", "name": "Gate"}]),
                )
            )
        events = (await admin_client.get(f"/api/v1/events?camera_id={camera.id}")).json()
        assert events["events"][0]["zones"] == [{"id": "gate", "name": "Gate"}]

    async def test_zones_saved_before_names_existed_are_shown_as_ignore_zones(
        self, admin_client: AsyncClient
    ) -> None:
        camera_id = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()["id"]
        async with session_scope() as session:
            camera = await session.get(Camera, camera_id)
            assert camera is not None
            camera.motion_zones = "[[[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]]"

        zones = (await admin_client.get(f"/api/v1/cameras/{camera_id}/motion-zones")).json()
        assert zones["zones"] == [
            {
                "id": "ignore-1",
                "name": "Ignored area 1",
                "kind": "ignore",
                "enabled": True,
                "sensitivity": None,
                "points": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]],
            }
        ]
