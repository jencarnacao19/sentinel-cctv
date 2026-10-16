"""Local Tapo control: capability discovery, safety, and the cloud question.

Every test here drives a fake vendor client. Nothing in this file opens a
socket, and nothing may reach a real camera: the whole point of the capability
layer is that Sentinel talks to hardware only through one seam, so the tests
replace that seam and assert on what crosses it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import SecretStr

from app.cameras import tapo
from app.cameras.tapo import (
    TapoAuthError,
    TapoManager,
    TapoUnavailableError,
    TapoUnsupportedError,
)
from app.config import Settings
from app.viewer.auth import VIEWER_KEY_HEADER

#: A camera Sentinel can positively identify as TP-Link. Every test in this
#: module is about what happens *after* that decision; the decision itself is
#: covered in ``test_camera_vendor.py``.
CAMERA_PAYLOAD = {
    "name": "Carport",
    "main_stream_url": "rtsp://192.168.50.217:554/stream1",
    "username": "camera-admin",
    "password": "camera-secret",
    "tapo_password": "tapo-control-secret",
    "manufacturer": "TP-Link",
    "model": "TC60",
}

#: A ``getMost`` answer shaped like a real one, for a camera that has a decent
#: but not unlimited feature set: LED, lens mask, motion and person, an SD card,
#: and no floodlight or siren.
#: A ``getMost`` answer shaped exactly like a real one: each method maps to a
#: *list* of result objects, and a method the camera does not implement is
#: pre-allocated as ``False`` rather than omitted. That distinction is the whole
#: basis of discovery, so the fixture reproduces it rather than simplifying it.
#:
#: This camera has an LED, a lens mask, motion and person detection and an SD
#: card. It has no floodlight, no siren and no baby-cry detection.
FAKE_MOST: dict[str, Any] = {
    "getDeviceInfo": [
        {
            "device_info": {
                "basic_info": {
                    "device_model": "C520WS",
                    "hw_version": "1.0",
                    "sw_version": "1.2.7 Build 240515",
                    "mac": "AA-BB-CC-DD-EE-FF",
                    "dev_id": "8021D4E5",
                    "ip": "192.168.50.217",
                }
            }
        }
    ],
    "getLedStatus": [{"led": {"config": {"enabled": "on"}}}],
    "getLensMaskConfig": [{"lens_mask": {"lens_mask_info": {"enabled": "off"}}}],
    "getDetectionConfig": [
        {"motion_detection": {"motion_det": {"enabled": "on", "digital_sensitivity": "60"}}}
    ],
    "getPersonDetectionConfig": [
        {"people_detection": {"detection": {"enabled": "on", "sensitivity": "high"}}}
    ],
    "getSdCardStatus": [
        {
            "harddisk_manage": {
                "hd_info": [
                    {
                        "hd_info_1": {
                            "status": "normal",
                            "total_space": "119.00",
                            "free_space": "12.34",
                        }
                    }
                ]
            }
        }
    ],
    # Answered with False: the camera was asked and said it has none of these.
    "getFloodlightStatus": False,
    "getFloodlightConfig": False,
    "getLastAlarmInfo": False,
    "getAlarmConfig": False,
    "getSirenStatus": False,
    "getBabyCryDetectionConfig": False,
}


#: How many ``multipleRequest`` round trips one healthy discovery costs.
#: Derived rather than written down, so growing the probe map cannot silently
#: make discovery more expensive without a test noticing.
_BATCHES_PER_DISCOVERY = -(-len(tapo._PROBE_REQUESTS) // tapo._PROBE_BATCH_SIZE)


class FakeTapo:
    """Stands in for ``pytapo.Tapo`` at the transport, not above it.

    The double implements ``performRequest`` -- the same ``multipleRequest``
    call the real client makes -- rather than a convenience wrapper, so the
    tests exercise Sentinel's own request building, batch splitting and response
    decoding instead of stepping over them.

    ``poison`` reproduces the failure that motivated all of that: firmware which
    rejects an entire batch because of one method it mishandles.
    """

    def __init__(
        self,
        *,
        most: dict[str, Any] | None = None,
        poison: set[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.batches: list[list[str]] = []
        self._most = FAKE_MOST if most is None else most
        self._poison = poison or set()

    def _record(self, name: str) -> None:
        self.calls.append(name)

    def performRequest(self, request: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        requests = request["params"]["requests"]
        methods = [entry["method"] for entry in requests]
        self.batches.append(methods)
        for method in methods:
            self._record(method)
        broken = self._poison.intersection(methods)
        if broken:
            # Exactly how real firmware fails: the whole answer is unusable, not
            # just the offending method's part of it.
            raise Exception(f"Unexpected camera response for {sorted(broken)}")
        responses = []
        for method in methods:
            payload = self._most.get(method, False)
            if isinstance(payload, list):
                for item in payload:
                    responses.append({"method": method, "error_code": 0, "result": item})
            elif payload is False:
                responses.append({"method": method, "error_code": -40106})
            else:
                responses.append({"method": method, "error_code": 0, "result": payload})
        return {"result": {"responses": responses}}

    def getBasicInfo(self) -> dict[str, Any]:  # noqa: N802 - vendor spelling
        self._record("getBasicInfo")
        return self._most.get("getDeviceInfo", {})

    def close(self) -> None:
        self._record("close")

    def __getattr__(self, name: str) -> Any:
        # Any setter the layer reaches for is accepted and recorded, which is
        # what lets a test assert that a *forbidden* one was never reached.
        def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            self._record(name)
            return {"error_code": 0}

        return call


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeTapo:
    """Replace the one seam that builds a vendor client."""
    client = FakeTapo()
    monkeypatch.setattr(tapo, "_connect", lambda target: client)
    # The host guard resolves DNS; the fixture camera uses a literal RFC1918
    # address, but pinning this keeps the tests off the network entirely.
    monkeypatch.setattr(tapo.netsafety, "ensure_scannable_host", _accept_host, raising=True)
    tapo.set_manager(TapoManager())
    return client


async def _accept_host(host: str) -> None:
    del host


async def _camera(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()


class TestCloudIndependence:
    """The claim "fully local" has to be provable, not asserted."""

    def test_the_three_cloud_methods_are_named_and_excluded(self) -> None:
        # Reading pytapo 3.4.18: every request goes to
        # _getControlHost() == f"{host}:{controlPort}". These three are the only
        # methods that make the *camera* reach TP-Link.
        assert (
            frozenset(
                {
                    "isUpdateAvailable",
                    "startFirmwareUpgrade",
                    "getFirmwareUpdateStatus",
                    # Not a cloud call, but destructive storage, excluded for
                    # the same reason: it is never Sentinel's to do.
                    "format",
                }
            )
            == tapo.FORBIDDEN_METHODS
        )

    def test_no_forbidden_method_is_ever_requested(self) -> None:
        # Stronger than the previous "omitted from the vendor's batch": Sentinel
        # now builds the request itself, so a cloud-touching method is not
        # excluded from a larger list, it is simply never asked for.
        assert tapo.FORBIDDEN_METHODS.isdisjoint(tapo._PROBE_REQUESTS)

    def test_every_probed_method_is_one_the_decoder_can_use(self) -> None:
        # A method in the request map that nothing reads is a round trip spent
        # on nothing, which on battery firmware is not free.
        read = {method for methods in tapo._FEATURE_METHODS.values() for method in methods}
        # getFloodlightConfig and the alarm reads feed state rather than a
        # capability of their own; everything else must back a capability.
        extra = {"getFloodlightConfig", "getAlarmConfig", "getLastAlarmInfo", "getSirenStatus"}
        assert set(tapo._PROBE_REQUESTS) <= read | extra

    def test_no_forbidden_method_appears_anywhere_in_the_module(self) -> None:
        from pathlib import Path

        source = Path(tapo.__file__).read_text(encoding="utf-8")
        for method in tapo.FORBIDDEN_METHODS:
            # The names appear only in the exclusion set and its comment, never
            # as a call.
            assert f"client.{method}(" not in source
            assert f".{method}()" not in source

    async def test_discovery_never_calls_a_cloud_method(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
        for method in tapo.FORBIDDEN_METHODS:
            assert method not in fake_client.calls


class TestCapabilityDiscovery:
    async def test_only_confirmed_features_are_reported(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
        assert response.status_code == 200, response.text
        payload = response.json()

        assert "led" in payload["supported"]
        assert "motion" in payload["supported"]
        assert "person" in payload["supported"]
        assert "sd_card" in payload["supported"]
        # The fake answered nothing for these, so they must be absent rather
        # than guessed from the model name.
        assert "floodlight" not in payload["supported"]
        assert "alarm" not in payload["supported"]
        assert "baby_cry" not in payload["supported"]

    async def test_the_destructive_operations_are_advertised_as_excluded(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        payload = (
            await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
        ).json()
        assert set(payload["excluded"]) == {
            "firmware_upgrade",
            "firmware_check",
            "sd_card_format",
            "factory_reset",
        }

    async def test_a_model_that_answers_nothing_supports_nothing(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown model must degrade to no controls, not to broken ones."""
        empty = FakeTapo(most={})
        monkeypatch.setattr(tapo, "_connect", lambda target: empty)
        monkeypatch.setattr(tapo.netsafety, "ensure_scannable_host", _accept_host)
        tapo.set_manager(TapoManager())

        camera = await _camera(admin_client)
        payload = (
            await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
        ).json()
        assert payload["supported"] == []

    async def test_discovery_is_cached_rather_than_repeated_per_request(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        """A small embedded HTTP server must not be polled on every page load."""
        camera = await _camera(admin_client)
        for _ in range(4):
            await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
        assert len(fake_client.batches) == _BATCHES_PER_DISCOVERY

    async def test_refresh_forces_a_new_probe(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
        await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities?refresh=true")
        assert len(fake_client.batches) == _BATCHES_PER_DISCOVERY * 2


class TestSecrecy:
    async def test_no_response_carries_a_credential_or_an_address(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        for path in ("capabilities", "state"):
            response = await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/{path}")
            body = response.text
            assert "tapo-control-secret" not in body
            assert "camera-secret" not in body
            assert "192.168.50.217" not in body
            assert "rtsp://" not in body
            # Identifiers the vendor returns that describe the installation
            # rather than the hardware.
            assert "AA-BB-CC-DD-EE-FF" not in body
            assert "8021D4E5" not in body

    async def test_the_hardware_description_still_reaches_the_administrator(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        payload = (
            await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
        ).json()
        assert payload["device"]["model"] == "C520WS"
        assert payload["device"]["firmware_version"].startswith("1.2.7")


class TestAuthorisation:
    async def test_every_route_requires_authentication(self, client: AsyncClient) -> None:
        for method, path in (
            ("get", "/api/v1/cameras/any/tapo/capabilities"),
            ("get", "/api/v1/cameras/any/tapo/state"),
            ("post", "/api/v1/cameras/any/tapo/led"),
            ("post", "/api/v1/cameras/any/tapo/siren/start"),
            ("post", "/api/v1/cameras/any/tapo/reboot"),
        ):
            response = (
                await client.post(path, json={}) if method == "post" else await client.get(path)
            )
            assert response.status_code == 401, path

    async def test_a_footage_operator_cannot_control_a_camera(
        self, admin_client: AsyncClient, settings: Settings, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        admin_client.headers.pop("Authorization", None)
        admin_client.cookies.clear()
        settings.viewer_enabled = True
        settings.viewer_key = SecretStr("t" * 48)
        admin_client.headers[VIEWER_KEY_HEADER] = "t" * 48

        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo/led", json={"enabled": True}
        )
        assert response.status_code == 401

    async def test_an_unknown_feature_is_not_a_vendor_method_name(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        """A path segment must never become a call on the vendor client."""
        camera = await _camera(admin_client)
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo/startFirmwareUpgrade", json={}
        )
        assert response.status_code == 404
        assert "startFirmwareUpgrade" not in fake_client.calls


class TestPrivacyMode:
    async def test_privacy_blocks_every_camera_control(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        enabled = await admin_client.put(
            "/api/v1/system/privacy", json={"enabled": True, "reason": "guests"}
        )
        assert enabled.status_code == 200, enabled.text

        calls_before = len(fake_client.calls)
        for method, path, body in (
            ("get", f"/api/v1/cameras/{camera['id']}/tapo/capabilities", None),
            ("get", f"/api/v1/cameras/{camera['id']}/tapo/state", None),
            ("post", f"/api/v1/cameras/{camera['id']}/tapo/led", {"enabled": True}),
            (
                "post",
                f"/api/v1/cameras/{camera['id']}/tapo/siren/stop",
                {"confirm": True},
            ),
            ("post", f"/api/v1/cameras/{camera['id']}/tapo/reboot", {"confirm": True}),
        ):
            call = getattr(admin_client, method)
            response = await call(path, json=body) if body is not None else await call(path)
            assert response.status_code == 423, path

        # Blocked before the network, not after it.
        assert len(fake_client.calls) == calls_before

    async def test_the_hardware_lens_mask_is_not_sentinel_privacy_mode(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        """Two different mechanisms on two different devices.

        Sentinel's privacy mode must keep working whether or not the camera has
        a lens mask, and turning the lens mask on must not be mistaken for it.
        """
        camera = await _camera(admin_client)
        state = (await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/state")).json()
        # The fixture camera reports its lens mask off.
        assert state["privacy_mask_enabled"] is False

        privacy_status = (await admin_client.get("/api/v1/system/privacy")).json()
        assert privacy_status["enabled"] is False


class TestDangerousActions:
    async def test_the_siren_refuses_without_an_explicit_confirmation(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo/siren/start", json={"confirm": False}
        )
        assert response.status_code == 400
        assert not any("Alarm" in call or "Siren" in call for call in fake_client.calls)

    async def test_a_reboot_refuses_without_an_explicit_confirmation(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo/reboot", json={"confirm": False}
        )
        assert response.status_code == 400
        assert "reboot" not in fake_client.calls

    async def test_a_camera_without_a_siren_is_refused_rather_than_attempted(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo/siren/start", json={"confirm": True}
        )
        # The fake advertises no alarm capability.
        assert response.status_code == 409

    async def test_extra_body_fields_are_refused(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo/reboot",
            json={"confirm": True, "force": True},
        )
        assert response.status_code == 422


class TestResilience:
    """A vendor failure must never become a recording failure."""

    async def test_an_authentication_failure_is_reported_not_retried_for_ever(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(target: Any) -> Any:
            raise Exception("Invalid authentication data")

        monkeypatch.setattr(tapo, "_connect", refuse)
        monkeypatch.setattr(tapo.netsafety, "ensure_scannable_host", _accept_host)
        tapo.set_manager(TapoManager())

        camera = await _camera(admin_client)
        response = await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
        assert response.status_code == 502
        # The vendor's own wording never reaches the client verbatim.
        assert "Invalid authentication data" not in response.text

    async def test_a_timeout_is_reported_as_unavailable(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Slow(FakeTapo):
            def performRequest(self, request: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
                del request
                raise TimeoutError

        monkeypatch.setattr(tapo, "_connect", lambda target: Slow())
        monkeypatch.setattr(tapo.netsafety, "ensure_scannable_host", _accept_host)
        tapo.set_manager(TapoManager())

        camera = await _camera(admin_client)
        response = await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
        assert response.status_code == 503

    async def test_a_public_host_is_refused_before_any_connection(
        self, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """These routes must not become a generic outbound request tool."""
        built: list[Any] = []

        async def refuse(host: str) -> None:
            raise tapo.netsafety.UnscannableHostError(host)

        monkeypatch.setattr(tapo.netsafety, "ensure_scannable_host", refuse)
        monkeypatch.setattr(tapo, "_connect", lambda target: built.append(target))
        tapo.set_manager(TapoManager())

        camera = await _camera(admin_client)
        response = await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
        assert response.status_code == 503
        assert built == []

    async def test_the_circuit_breaker_stops_hammering_a_dead_camera(self) -> None:
        attempts = 0

        def fail(target: Any) -> Any:
            nonlocal attempts
            attempts += 1
            raise TimeoutError

        manager = TapoManager()
        target = tapo._Target(
            camera_id="cam",
            camera_name="Carport",
            host="192.168.50.5",
            user="admin",
            password="secret",
            fingerprint=tapo._fingerprint("192.168.50.5", "admin", "secret"),
        )

        async def run() -> None:
            await manager._invoke(
                target, "probe", lambda client: client.performRequest({}), timeout_seconds=0.5
            )

        # Enough consecutive failures to trip the breaker, then one more call
        # that must not reach the network at all.
        for _ in range(tapo.BREAKER_FAILURE_THRESHOLD + 1):
            with pytest.raises((TapoUnavailableError, TapoAuthError)):
                await run()

        before = attempts
        with pytest.raises(TapoUnavailableError):
            await run()
        assert attempts == before, "an open breaker must not dial the camera"


class TestSensitivityConventions:
    """Each detection family takes a different argument shape, and getting it
    wrong raises a bare ``Exception`` from the vendor library at runtime."""

    def test_a_numeric_family_keeps_a_number(self) -> None:
        family = tapo._FAMILIES_BY_FEATURE["package"]
        assert tapo._sensitivity_for(family, 70) == 70

    def test_a_label_only_family_never_receives_a_number(self) -> None:
        family = tapo._FAMILIES_BY_FEATURE["tamper"]
        value = tapo._sensitivity_for(family, 70)
        assert isinstance(value, str)
        assert value in {"low", "normal", "high", "medium"}

    def test_a_family_that_takes_no_sensitivity_refuses_one(self) -> None:
        """Refused explicitly rather than silently dropped: a caller who sent a
        sensitivity to line-crossing has misunderstood something, and telling
        them is more useful than quietly ignoring half their request."""
        family = tapo._FAMILIES_BY_FEATURE["line_crossing"]
        with pytest.raises(TapoUnsupportedError):
            tapo._sensitivity_for(family, 70)


class TestLifecycle:
    async def test_closing_a_camera_releases_its_session(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        await admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")

        await tapo.get_manager().close(camera["id"])
        assert "close" in fake_client.calls

    async def test_close_all_is_safe_with_nothing_open(self) -> None:
        await TapoManager().close_all()

    async def test_one_session_is_reused_across_concurrent_calls(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        """Per-camera locking: a burst must not build several clients."""
        camera = await _camera(admin_client)
        await asyncio.gather(
            *(
                admin_client.get(f"/api/v1/cameras/{camera['id']}/tapo/capabilities")
                for _ in range(5)
            )
        )
        assert len(fake_client.batches) == _BATCHES_PER_DISCOVERY


class TestUnsupportedWrites:
    async def test_writing_an_unsupported_feature_is_a_conflict(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo/floodlight", json={"enabled": True}
        )
        assert response.status_code == 409

    async def test_a_supported_write_reaches_the_camera_and_reports_the_state(
        self, admin_client: AsyncClient, fake_client: FakeTapo
    ) -> None:
        camera = await _camera(admin_client)
        response = await admin_client.post(
            f"/api/v1/cameras/{camera['id']}/tapo/led", json={"enabled": False}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["applied"] is True
        assert payload["feature"] == "led"
        assert payload["state"] is not None
        assert any("Led" in call or "LED" in call for call in fake_client.calls)


def test_the_unsupported_error_type_exists_for_the_router_contract() -> None:
    assert issubclass(TapoUnsupportedError, tapo.TapoError)
