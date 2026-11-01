"""Battery recordings started by a wake or by motion elsewhere, not by a person.

The timed-recording manager is replaced by a recorder of requests: what these
tests care about is *when* a bounded session is asked for, and for how long.
The session itself is covered in ``test_tapo_timed``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient

from app.cameras import tapo_auto, tapo_timed
from app.cameras.tapo_auto import AutoRecorder
from app.config import Settings
from app.database.models import Camera
from app.system import privacy

BATTERY_PAYLOAD = {
    "name": "Shed",
    "kind": "tapo_battery",
    "host": "192.168.50.20",
    "tapo_password": "local-third-party-secret",
}


class FakeTimed:
    def __init__(self) -> None:
        self.begins: list[tuple[str, int, str]] = []
        self.busy = False

    def active(self, _camera_id: str) -> bool:
        return self.busy

    async def begin(
        self,
        camera: Camera,
        *,
        duration_seconds: int,
        battery_percent: int | None,
        requested_by: str,
    ) -> None:
        del battery_percent
        self.begins.append((camera.id, duration_seconds, requested_by))


@pytest.fixture
def timed(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> FakeTimed:
    monkeypatch.setattr(settings, "enable_tapo_battery_provider", True)
    fake = FakeTimed()
    tapo_timed.set_manager(fake)  # type: ignore[arg-type]
    return fake


class Pings:
    """Answers the recorder's pings from a script, and counts them."""

    def __init__(self) -> None:
        self.awake = False
        self.hosts: list[str] = []

    async def __call__(self, host: str) -> bool:
        self.hosts.append(host)
        return self.awake


@pytest.fixture
def pings(monkeypatch: pytest.MonkeyPatch) -> Pings:
    fake = Pings()
    monkeypatch.setattr(tapo_auto, "ping", fake)
    return fake


async def _camera(client: AsyncClient, **extra: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/cameras", json={**BATTERY_PAYLOAD, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def _rules(client: AsyncClient, camera_id: str, **rules: Any) -> Any:
    return await client.put(f"/api/v1/cameras/{camera_id}/tapo-battery/auto-recording", json=rules)


async def _settle(recorder: AutoRecorder) -> None:
    await asyncio.gather(*recorder._tasks)


async def _poll(recorder: AutoRecorder, pings: Pings, at: float, *, awake: bool) -> None:
    pings.awake = awake
    await recorder.poll_once(now=at)
    await _settle(recorder)


class TestRules:
    async def test_everything_is_off_until_switched_on(
        self, admin_client: AsyncClient, timed: FakeTimed
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.get(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/auto-recording"
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "on_wake": False,
            "on_wake_minutes": 2,
            "on_motion": False,
            "on_motion_minutes": 2,
            "on_wake_supported": True,
        }

    async def test_rules_are_saved_per_camera(
        self, admin_client: AsyncClient, timed: FakeTimed
    ) -> None:
        camera = await _camera(admin_client)
        saved = await _rules(
            admin_client,
            camera["id"],
            on_wake=True,
            on_wake_minutes=2,
            on_motion=True,
            on_motion_minutes=5,
        )
        assert saved.status_code == 200, saved.text
        again = await admin_client.get(
            f"/api/v1/cameras/{camera['id']}/tapo-battery/auto-recording"
        )
        assert again.json()["on_wake"] is True
        assert again.json()["on_motion_minutes"] == 5

    async def test_a_duration_above_the_ceiling_is_refused(
        self, admin_client: AsyncClient, timed: FakeTimed
    ) -> None:
        camera = await _camera(admin_client)
        await admin_client.put(
            "/api/v1/cameras/tapo-battery/timed-recording/max-minutes", json={"max_minutes": 10}
        )
        response = await _rules(admin_client, camera["id"], on_motion=True, on_motion_minutes=30)
        assert response.status_code == 422

    async def test_a_hub_camera_cannot_be_watched_for_wakes(
        self, admin_client: AsyncClient, timed: FakeTimed
    ) -> None:
        camera = await _camera(admin_client, tapo_child_id="8022000000000003")
        read = await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo-battery/auto-recording")
        assert read.json()["on_wake_supported"] is False
        assert (await _rules(admin_client, camera["id"], on_wake=True)).status_code == 422
        assert (await _rules(admin_client, camera["id"], on_motion=True)).status_code == 200

    async def test_only_an_administrator_may_change_them(
        self, client: AsyncClient, timed: FakeTimed
    ) -> None:
        response = await _rules(client, "any", on_wake=True)
        assert response.status_code == 401


class TestWakes:
    async def test_a_wake_that_lasts_is_recorded_once(
        self, admin_client: AsyncClient, timed: FakeTimed, pings: Pings
    ) -> None:
        camera = await _camera(admin_client)
        await _rules(admin_client, camera["id"], on_wake=True, on_wake_minutes=2)
        recorder = AutoRecorder()

        for at in (0, 2, 4):
            await _poll(recorder, pings, at, awake=True)
        assert timed.begins == [], "a few seconds awake is the camera checking in"

        await _poll(recorder, pings, 6, awake=True)
        assert timed.begins == [(camera["id"], 120, "automatic: woke up on its own")]

        for at in (8, 10, 12):
            await _poll(recorder, pings, at, awake=True)
        assert len(timed.begins) == 1, "one wake, one recording"
        assert pings.hosts[0] == "192.168.50.20"

    async def test_the_next_wake_is_recorded_again(
        self, admin_client: AsyncClient, timed: FakeTimed, pings: Pings
    ) -> None:
        camera = await _camera(admin_client)
        await _rules(admin_client, camera["id"], on_wake=True)
        recorder = AutoRecorder()
        for at in (0, 2, 4, 6):
            await _poll(recorder, pings, at, awake=True)
        for at in (8, 10, 12):
            await _poll(recorder, pings, at, awake=False)
        for at in (14, 16, 18, 20):
            await _poll(recorder, pings, at, awake=True)
        assert len(timed.begins) == 2

    async def test_one_lost_reply_is_not_a_new_wake(
        self, admin_client: AsyncClient, timed: FakeTimed, pings: Pings
    ) -> None:
        camera = await _camera(admin_client)
        await _rules(admin_client, camera["id"], on_wake=True)
        recorder = AutoRecorder()
        await _poll(recorder, pings, 0, awake=True)
        await _poll(recorder, pings, 2, awake=True)
        await _poll(recorder, pings, 4, awake=False)
        # A missed reply costs a timeout, so the next one comes 5 s later.
        await _poll(recorder, pings, 7, awake=True)
        assert len(timed.begins) == 1

    async def test_a_wake_while_busy_is_not_recorded_afterwards(
        self, admin_client: AsyncClient, timed: FakeTimed, pings: Pings
    ) -> None:
        """A live view wakes the camera too; closing it must not start a recording."""
        camera = await _camera(admin_client)
        await _rules(admin_client, camera["id"], on_wake=True)
        recorder = AutoRecorder()
        timed.busy = True
        for at in (0, 2, 4, 6):
            await _poll(recorder, pings, at, awake=True)
        timed.busy = False
        for at in (8, 10):
            await _poll(recorder, pings, at, awake=True)
        assert timed.begins == []

    async def test_nothing_is_pinged_unless_switched_on_or_for_a_hub_camera(
        self, admin_client: AsyncClient, timed: FakeTimed, pings: Pings
    ) -> None:
        await _camera(admin_client)
        hub = await _camera(admin_client, name="Gate", tapo_child_id="8022000000000003")
        await _rules(admin_client, hub["id"], on_motion=True)
        recorder = AutoRecorder()
        await _poll(recorder, pings, 0, awake=True)
        assert pings.hosts == []

    async def test_privacy_mode_stops_the_pings(
        self,
        admin_client: AsyncClient,
        timed: FakeTimed,
        pings: Pings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        camera = await _camera(admin_client)
        await _rules(admin_client, camera["id"], on_wake=True)
        monkeypatch.setattr(privacy.get_manager(), "_state", privacy.PrivacyState(enabled=True))
        await _poll(AutoRecorder(), pings, 0, awake=True)
        assert pings.hosts == []


class TestMotionElsewhere:
    async def test_motion_on_another_camera_wakes_and_records(
        self, admin_client: AsyncClient, timed: FakeTimed
    ) -> None:
        camera = await _camera(admin_client)
        await _rules(admin_client, camera["id"], on_motion=True, on_motion_minutes=5)
        recorder = AutoRecorder()
        await recorder.on_motion("driveway-id", "Driveway")
        await _settle(recorder)
        assert timed.begins == [(camera["id"], 300, "automatic: motion on Driveway")]

    async def test_switched_off_or_busy_means_no_recording(
        self, admin_client: AsyncClient, timed: FakeTimed
    ) -> None:
        camera = await _camera(admin_client)
        recorder = AutoRecorder()
        await recorder.on_motion("driveway-id", "Driveway")
        await _rules(admin_client, camera["id"], on_motion=True)
        timed.busy = True
        await recorder.on_motion("driveway-id", "Driveway")
        await _settle(recorder)
        assert timed.begins == []

    async def test_a_published_motion_event_reaches_the_recorder(
        self, admin_client: AsyncClient, timed: FakeTimed
    ) -> None:
        from app.events.bus import MOTION_STARTED, publish

        camera = await _camera(admin_client)
        await _rules(admin_client, camera["id"], on_motion=True)
        recorder = AutoRecorder()
        await recorder.start()
        try:
            publish(MOTION_STARTED, {"camera_id": "driveway-id", "camera": "Driveway"})
            for _ in range(50):
                if timed.begins:
                    break
                await asyncio.sleep(0.02)
        finally:
            await recorder.stop()
        assert [begin[0] for begin in timed.begins] == [camera["id"]]
