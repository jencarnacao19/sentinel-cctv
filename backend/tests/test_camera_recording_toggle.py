"""Turning one camera's recorder off, and proving it really stopped.

The distinction this file exists to enforce: a toggle that flips a column is not
a recording toggle. Every test that claims a camera is recording asserts against
the recorder set the manager actually supervises, and the process double is
installed at the ``SupervisedProcess`` boundary -- the last point before FFmpeg
-- so the manager's own locking, bookkeeping, health counting and events all run
for real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from app.cameras.service import list_cameras
from app.database.session import session_scope
from app.recording import manager as recording_manager
from app.recording.manager import get_manager
from app.system import privacy

CAMERA_A = {
    "name": "Carport",
    "main_stream_url": "rtsp://192.168.50.217:554/stream1",
    "username": "camera-admin",
    "password": "camera-secret",
}
CAMERA_B = {
    "name": "Courtyard",
    "main_stream_url": "rtsp://192.168.50.179:554/Streaming/Channels/101",
    "username": "admin",
    "password": "nvr-secret",
}


class FakeProcess:
    """A supervised process that starts and stops without spawning FFmpeg.

    Everything above it -- ``apply_camera_recording``, the per-camera lock, the
    recorder map, ``camera_recording_state``, the health probe -- is the real
    implementation.
    """

    instances: ClassVar[list[FakeProcess]] = []

    def __init__(self, name: str, build_command: Callable[[], list[str]]) -> None:
        self.name = name
        self.build_command = build_command
        self.started = 0
        self.stopped = 0
        self.running = False
        self.fail_on_start = False
        self.fail_on_stop = False
        self.last_uptime_seconds = 0.0
        self.last_exit_expected = True
        FakeProcess.instances.append(self)

    async def start(self) -> None:
        self.started += 1
        if self.fail_on_start:
            raise RuntimeError("ffmpeg refused to start")
        # Built for real: a camera with no usable source raises here, which is
        # exactly the failure the toggle has to report honestly.
        self.build_command()
        self.running = True

    async def stop(self) -> None:
        self.stopped += 1
        if self.fail_on_stop:
            raise RuntimeError("ffmpeg did not stop")
        self.running = False

    @property
    def is_running(self) -> bool:
        return self.running

    def status(self) -> dict[str, object]:
        return {"name": self.name, "running": self.running, "restarts": 0}


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch) -> list[FakeProcess]:
    FakeProcess.instances = []

    def factory(
        name: str,
        build_command: Callable[[], list[str]],
        env: dict[str, str],
        on_exit: Callable[[int | None], Awaitable[None]],
        on_output: Callable[[str], bool],
    ) -> Any:
        del env, on_exit, on_output
        return FakeProcess(name, build_command)

    monkeypatch.setattr(get_manager(), "_recorder_factory", factory)
    return FakeProcess.instances


async def _create(client: AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post("/api/v1/cameras", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _start_all() -> None:
    """Bring every configured recorder up through the real manager path."""
    async with session_scope() as session:
        cameras = await list_cameras(session)
    for camera in cameras:
        await get_manager().apply_camera_recording(camera)


class TestToggle:
    async def test_disabling_stops_only_that_camera(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        first = await _create(admin_client, CAMERA_A)
        second = await _create(admin_client, CAMERA_B)
        await _start_all()
        manager = get_manager()
        assert manager.is_recording(first["id"])
        assert manager.is_recording(second["id"])

        response = await admin_client.put(
            f"/api/v1/cameras/{first['id']}/recording",
            json={"enabled": False, "confirm": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "off"
        assert response.json()["recording_enabled"] is False

        assert not manager.is_recording(first["id"]), "the recorder must actually be gone"
        assert manager.is_recording(second["id"]), "the other camera must be untouched"
        assert len(recorders) == 2, "no recorder was rebuilt for the untouched camera"

    async def test_enabling_restarts_only_that_camera(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await _start_all()
        await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": False, "confirm": True}
        )
        response = await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "recording"
        assert get_manager().is_recording(camera["id"])
        assert len(recorders) == 2, "re-enabling builds a fresh recorder"

    async def test_the_setting_is_persisted_and_survives_a_reload(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": False, "confirm": True}
        )
        reloaded = (await admin_client.get(f"/api/v1/cameras/{camera['id']}")).json()
        assert reloaded["recording_enabled"] is False
        assert reloaded["recording_state"] == "off"

        async with session_scope() as session:
            cameras = await list_cameras(session)
            assert cameras[0].recording_enabled is False
            # The authoritative predicate, not just the column.
            assert get_manager()._should_record(cameras[0]) is False

    async def test_disabling_without_confirmation_is_refused(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await _start_all()
        response = await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": False}
        )
        assert response.status_code == 400
        assert get_manager().is_recording(camera["id"]), "a refused request changes nothing"

    async def test_a_failed_stop_is_reported_and_never_claimed_as_off(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await _start_all()
        recorders[0].fail_on_stop = True

        response = await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording",
            json={"enabled": False, "confirm": True},
        )

        assert response.status_code == 503, response.text
        assert "still active" in response.json()["detail"]
        assert get_manager().is_recording(camera["id"])
        reloaded = (await admin_client.get(f"/api/v1/cameras/{camera['id']}")).json()
        assert reloaded["recording_enabled"] is True
        assert reloaded["recording_state"] == "recording"

    async def test_patch_cannot_bypass_the_stop_confirmation(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await _start_all()
        response = await admin_client.patch(
            f"/api/v1/cameras/{camera['id']}", json={"recording_enabled": False}
        )
        assert response.status_code == 422
        assert get_manager().is_recording(camera["id"])

    async def test_enabling_needs_no_confirmation(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        response = await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": True}
        )
        assert response.status_code == 200

    async def test_it_does_not_touch_live_view_or_motion(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": False, "confirm": True}
        )
        reloaded = (await admin_client.get(f"/api/v1/cameras/{camera['id']}")).json()
        assert reloaded["enabled"] is True, "the camera itself stays enabled"
        assert reloaded["motion_enabled"] is True
        assert reloaded["main_stream_url"] == CAMERA_A["main_stream_url"]

    async def test_it_does_not_touch_global_privacy_mode(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": False, "confirm": True}
        )
        assert privacy.get_manager().state.enabled is False
        status_response = await admin_client.get("/api/v1/system/privacy")
        assert status_response.json()["enabled"] is False


class TestHonestState:
    async def test_a_failed_start_is_reported_as_failed_not_as_recording(
        self, admin_client: AsyncClient, recorders: list[FakeProcess], monkeypatch: Any
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": False, "confirm": True}
        )

        original = recording_manager.RecordingManager._start_recorder

        async def failing(self: Any, cam: Any) -> None:
            await original(self, cam)
            raise RuntimeError("the recorder could not be started")

        monkeypatch.setattr(recording_manager.RecordingManager, "_start_recorder", failing)

        response = await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["recording_enabled"] is True
        assert response.json()["state"] == "failed", "a failed start must not read as recording"
        assert "could not start" in response.json()["message"]
        assert not get_manager().is_recording(camera["id"])

    async def test_a_dead_recorder_entry_is_not_left_behind_after_a_failed_start(
        self, admin_client: AsyncClient, recorders: list[FakeProcess], monkeypatch: Any
    ) -> None:
        """A stale entry would keep being counted as running by health."""
        camera = await _create(admin_client, CAMERA_A)
        await admin_client.put(
            f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": False, "confirm": True}
        )

        async def failing(self: Any, cam: Any) -> None:
            raise RuntimeError("no")

        monkeypatch.setattr(recording_manager.RecordingManager, "_start_recorder", failing)
        await admin_client.put(f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": True})
        assert camera["id"] not in get_manager()._recorders

    async def test_privacy_mode_is_reported_as_paused_not_as_failure(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await _start_all()
        manager = get_manager()
        # The real pause path, which stops the recorders as well as setting the
        # flag. A camera whose recorder privacy stopped is paused, not broken.
        await manager.set_privacy_paused(True)
        try:
            assert not manager.is_recording(camera["id"])
            reloaded = (await admin_client.get(f"/api/v1/cameras/{camera['id']}")).json()
            assert reloaded["recording_state"] == "paused"
            assert reloaded["recording_enabled"] is True, "privacy does not rewrite the setting"
        finally:
            manager.initialise_privacy(False)

    async def test_the_health_probe_stops_expecting_a_disabled_camera(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        first = await _create(admin_client, CAMERA_A)
        await _create(admin_client, CAMERA_B)
        await _start_all()

        before = await get_manager().health_probe()
        assert before.data == {"expected": 2, "running": 2}

        await admin_client.put(
            f"/api/v1/cameras/{first['id']}/recording", json={"enabled": False, "confirm": True}
        )
        after = await get_manager().health_probe()
        assert after.data == {"expected": 1, "running": 1}
        assert after.state.value == "ok", "a deliberately disabled camera is not a fault"


class TestRaces:
    async def test_two_simultaneous_disables_leave_one_stopped_recorder(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await _start_all()
        responses = await asyncio.gather(
            *(
                admin_client.put(
                    f"/api/v1/cameras/{camera['id']}/recording",
                    json={"enabled": False, "confirm": True},
                )
                for _ in range(4)
            )
        )
        assert all(response.status_code == 200 for response in responses)
        assert all(response.json()["state"] == "off" for response in responses)
        assert not get_manager().is_recording(camera["id"])
        assert recorders[0].stopped == 1, "the recorder is stopped once, not four times"

    async def test_interleaved_enable_and_disable_converge_on_the_last_setting(
        self, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await _start_all()
        await asyncio.gather(
            admin_client.put(
                f"/api/v1/cameras/{camera['id']}/recording",
                json={"enabled": False, "confirm": True},
            ),
            admin_client.put(f"/api/v1/cameras/{camera['id']}/recording", json={"enabled": True}),
        )
        final = (await admin_client.get(f"/api/v1/cameras/{camera['id']}")).json()
        # Whichever request landed last, the recorder and the stored setting
        # must agree. A toggle whose column and process disagree is the failure
        # mode this whole file exists to prevent.
        assert get_manager().is_recording(camera["id"]) is final["recording_enabled"]
        assert final["recording_state"] == ("recording" if final["recording_enabled"] else "off")


class TestBoundaries:
    async def test_it_is_administrator_only(
        self, app: Any, admin_client: AsyncClient, recorders: list[FakeProcess]
    ) -> None:
        camera = await _create(admin_client, CAMERA_A)
        await _start_all()
        # A genuinely separate client: `admin_client` is the same object as
        # `client` and keeps the session cookie, so dropping the header alone
        # would not prove anything.
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://sentinel.test") as anonymous:
            response = await anonymous.put(
                f"/api/v1/cameras/{camera['id']}/recording",
                json={"enabled": False, "confirm": True},
            )
        assert response.status_code == 401
        assert get_manager().is_recording(camera["id"]), "an unauthorised call changes nothing"

    async def test_a_battery_camera_has_no_continuous_recorder_to_toggle(
        self, admin_client: AsyncClient, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "enable_tapo_battery_provider", True)
        created = await admin_client.post(
            "/api/v1/cameras",
            json={
                "name": "Patio",
                "kind": "tapo_battery",
                "host": "192.168.50.220",
                "tapo_password": "local-secret",
            },
        )
        assert created.status_code == 201, created.text
        response = await admin_client.put(
            f"/api/v1/cameras/{created.json()['id']}/recording",
            json={"enabled": False, "confirm": True},
        )
        assert response.status_code == 409

    async def test_an_unknown_camera_is_a_not_found(self, admin_client: AsyncClient) -> None:
        response = await admin_client.put(
            "/api/v1/cameras/00000000-0000-0000-0000-000000000000/recording",
            json={"enabled": True},
        )
        assert response.status_code == 404
