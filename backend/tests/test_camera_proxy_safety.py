"""Controls that survive each other behind a reverse proxy.

A failure motivated this file. It put Cloudflare's *"the origin
web server returned an invalid or incomplete response"* in front of the
operator while having nothing to do with Cloudflare: one control request could
occupy a vendor worker thread for tens of seconds, and those threads came from
the interpreter's shared pool, so a camera that had stopped answering slowed
every other request in the application until the proxy gave up on the origin.

The tests below assert the properties that make it impossible: the vendor pool
is separate from everyone else's, and no request can outlive its deadline.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from httpx import AsyncClient

from app.cameras import tapo, vendor_pool
from app.cameras.tapo import TapoManager

from .test_tapo import FakeTapo, _accept_host

TAPO_CAMERA = {
    "name": "Carport",
    "main_stream_url": "rtsp://192.168.50.217:554/stream1",
    "username": "camera-admin",
    "password": "camera-secret",
    "tapo_password": "tapo-control-secret",
    "manufacturer": "TP-Link",
    "model": "TC60",
}


class TestVendorThreadIsolation:
    """Vendor work must not run on the pool everything else shares."""

    def test_the_pool_is_separate_from_the_interpreter_default(self) -> None:
        pool = vendor_pool.get_executor(tapo.VENDOR_THREAD_LIMIT)
        assert pool._max_workers == tapo.VENDOR_THREAD_LIMIT
        assert pool._thread_name_prefix == "vendor"

    def test_no_vendor_module_reaches_for_the_shared_executor(self) -> None:
        """``asyncio.to_thread`` here would reintroduce the starvation.

        ``ptz`` is in this list because it was missed the first time. Its patrol
        restoration constructs a ``Tapo`` client -- a blocking, uncancellable
        network call -- and retries every thirty minutes against a camera whose
        credentials may be rejected, which is the worst possible shape for the
        shared pool. It was found still doing so after the rest of
        the fix had shipped.
        """
        from pathlib import Path

        from app.cameras import ptz

        for module in (tapo, ptz):
            path = module.__file__
            assert path is not None
            source = Path(path).read_text(encoding="utf-8")
            calls = [
                line
                for line in source.splitlines()
                if "asyncio.to_thread(" in line and not line.strip().startswith("#")
            ]
            assert calls == [], f"{module.__name__} still uses the shared executor: {calls}"

    async def test_a_stuck_vendor_call_does_not_delay_unrelated_thread_work(self) -> None:
        """The measured failure, reproduced and then prevented.

        Enough blocked vendor calls to fill the dedicated pool several times
        over, while an ordinary ``asyncio.to_thread`` -- the primitive every
        request handler in the application uses for ffprobe, snapshots and
        storage statistics -- must still complete promptly.
        """
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def blocked() -> None:
            asyncio.run_coroutine_threadsafe(_wait(release), loop).result(timeout=30)

        async def _wait(event: asyncio.Event) -> None:
            await event.wait()

        stuck = [
            asyncio.create_task(vendor_pool.run(blocked, max_workers=tapo.VENDOR_THREAD_LIMIT))
            for _ in range(tapo.VENDOR_THREAD_LIMIT * 3)
        ]
        await asyncio.sleep(0.2)
        try:
            began = time.monotonic()
            await asyncio.wait_for(asyncio.to_thread(time.sleep, 0.01), timeout=5.0)
            assert time.monotonic() - began < 2.0
        finally:
            release.set()
            await asyncio.gather(*stuck, return_exceptions=True)


class TestBoundedRequests:
    """No control request may outlive the window a proxy gives the origin."""

    def test_the_deadline_is_well_under_a_typical_origin_window(self) -> None:
        assert tapo.REQUEST_DEADLINE_SECONDS <= 20.0
        # Also under the browser client's own abort, so the operator sees a
        # Sentinel message rather than a generic client-side timeout.
        assert tapo.REQUEST_DEADLINE_SECONDS < 20.0

    def test_one_attempt_cannot_exceed_the_deadline_on_its_own(self) -> None:
        worst_attempt = tapo.DISCOVERY_TIMEOUT_SECONDS + tapo.CONNECT_TIMEOUT_SECONDS
        assert worst_attempt <= tapo.REQUEST_DEADLINE_SECONDS

    async def test_a_silent_camera_is_answered_inside_the_deadline(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The camera never replies. Sentinel must still answer, and quickly."""
        monkeypatch.setattr(tapo, "CONNECT_TIMEOUT_SECONDS", 0.4)
        monkeypatch.setattr(tapo, "DISCOVERY_TIMEOUT_SECONDS", 0.4)
        monkeypatch.setattr(tapo, "REQUEST_DEADLINE_SECONDS", 2.0)

        class Silent(FakeTapo):
            def performRequest(self, request: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
                del request
                time.sleep(30)
                raise AssertionError("unreachable")

        monkeypatch.setattr(tapo, "_connect", lambda target: Silent())
        monkeypatch.setattr(tapo.netsafety, "ensure_scannable_host", _accept_host)
        tapo.set_manager(TapoManager())

        created = (await admin_client.post("/api/v1/cameras", json=TAPO_CAMERA)).json()
        began = time.monotonic()
        response = await admin_client.get(
            f"/api/v1/cameras/{created['id']}/tapo/capabilities?refresh=true"
        )
        elapsed = time.monotonic() - began
        assert response.status_code == 503
        assert elapsed < 6.0, f"the origin took {elapsed:.1f}s to answer a silent camera"
        assert response.json()["detail"] == "The camera did not answer its local control service."


class TestPartialCapabilities:
    """One capability failing must not take the panel with it."""

    @pytest.fixture
    def poisoned(self, monkeypatch: pytest.MonkeyPatch) -> FakeTapo:
        """Firmware that rejects any batch containing ``getPresetConfig``.

        A real reported failure: the vendor helper raises on an unexpected
        number of ``getPresetConfig`` responses and the whole discovery is lost.
        """
        client = FakeTapo(poison={"getPresetConfig"})
        monkeypatch.setattr(tapo, "_connect", lambda target: client)
        monkeypatch.setattr(tapo.netsafety, "ensure_scannable_host", _accept_host)
        tapo.set_manager(TapoManager())
        return client

    async def test_the_other_capabilities_still_arrive(
        self, admin_client: AsyncClient, poisoned: FakeTapo
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=TAPO_CAMERA)).json()
        response = await admin_client.get(f"/api/v1/cameras/{created['id']}/tapo/capabilities")
        assert response.status_code == 200, response.text
        body = response.json()
        assert "led" in body["supported"]
        assert "privacy_mask" in body["supported"]
        assert "motion" in body["supported"]
        assert "sd_card" in body["supported"]
        assert "presets" not in body["supported"], "the failing probe is not claimed as supported"
        assert body["partial"] is True
        assert "getPresetConfig" in body["unchecked"]

    async def test_the_failure_is_isolated_to_the_offending_method(
        self, admin_client: AsyncClient, poisoned: FakeTapo
    ) -> None:
        """Batch splitting must converge on one method, not give up on eight."""
        created = (await admin_client.post("/api/v1/cameras", json=TAPO_CAMERA)).json()
        await admin_client.get(f"/api/v1/cameras/{created['id']}/tapo/capabilities")
        response = await admin_client.get(f"/api/v1/cameras/{created['id']}/tapo/capabilities")
        body = response.json()
        assert body["unchecked"] == ["getPresetConfig"]

    async def test_the_state_read_still_works_on_a_partial_camera(
        self, admin_client: AsyncClient, poisoned: FakeTapo
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=TAPO_CAMERA)).json()
        response = await admin_client.get(f"/api/v1/cameras/{created['id']}/tapo/state")
        assert response.status_code == 200, response.text
        assert response.json()["led_enabled"] is True
        assert response.json()["preset_count"] is None

    async def test_a_capability_confirmed_before_survives_a_later_silent_probe(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timed-out probe must not remove a control the operator was using."""
        healthy = FakeTapo()
        monkeypatch.setattr(tapo, "_connect", lambda target: healthy)
        monkeypatch.setattr(tapo.netsafety, "ensure_scannable_host", _accept_host)
        manager = TapoManager()
        tapo.set_manager(manager)

        created = (await admin_client.post("/api/v1/cameras", json=TAPO_CAMERA)).json()
        first = await admin_client.get(f"/api/v1/cameras/{created['id']}/tapo/capabilities")
        assert "led" in first.json()["supported"]

        # The LED probe now fails while everything else keeps answering.
        healthy._poison = {"getLedStatus"}
        second = await admin_client.get(
            f"/api/v1/cameras/{created['id']}/tapo/capabilities?refresh=true"
        )
        assert second.status_code == 200, second.text
        assert "led" in second.json()["supported"], "a control is not dropped on one bad probe"
        assert second.json()["partial"] is True
        assert "getLedStatus" in second.json()["unchecked"]

    async def test_a_camera_that_answers_nothing_is_still_an_error(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partial tolerance must not turn a dead camera into a green panel."""
        dead = FakeTapo(poison=set(tapo._PROBE_REQUESTS))
        monkeypatch.setattr(tapo, "_connect", lambda target: dead)
        monkeypatch.setattr(tapo.netsafety, "ensure_scannable_host", _accept_host)
        tapo.set_manager(TapoManager())

        created = (await admin_client.post("/api/v1/cameras", json=TAPO_CAMERA)).json()
        response = await admin_client.get(f"/api/v1/cameras/{created['id']}/tapo/capabilities")
        assert response.status_code == 503


class TestSanitisedErrors:
    """Nothing a vendor library said reaches the operator verbatim."""

    async def test_a_vendor_exception_becomes_one_of_sentinels_own_messages(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "tapo-control-secret"

        class Leaky(FakeTapo):
            def performRequest(self, request: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
                del request
                raise Exception(
                    f"HTTPSConnectionPool(host='192.168.50.217', port=443): password={secret}"
                )

        monkeypatch.setattr(tapo, "_connect", lambda target: Leaky())
        monkeypatch.setattr(tapo.netsafety, "ensure_scannable_host", _accept_host)
        tapo.set_manager(TapoManager())

        created = (await admin_client.post("/api/v1/cameras", json=TAPO_CAMERA)).json()
        response = await admin_client.get(f"/api/v1/cameras/{created['id']}/tapo/capabilities")
        assert response.status_code == 503
        assert secret not in response.text
        assert "192.168.50.217" not in response.text
        assert "HTTPSConnectionPool" not in response.text
        assert response.json()["detail"] in {
            "The camera did not answer its local control service.",
            "The camera rejected the stored Tapo credentials.",
        }

    async def test_the_sanitised_vocabulary_is_actionable(self) -> None:
        """Each message has to tell the operator what to do next."""
        messages = {
            tapo._AUTH_MESSAGE,
            tapo._UNAVAILABLE_MESSAGE,
            tapo._UNSUPPORTED_MESSAGE,
            tapo._NOT_CONFIGURED_MESSAGE,
            tapo._PRIVATE_HOST_MESSAGE,
        }
        for message in messages:
            assert message.endswith("."), message
            assert "Exception" not in message
            assert "Traceback" not in message
            assert "Cloudflare" not in message
            assert "origin" not in message.lower()
