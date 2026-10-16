"""Which cameras get vendor-specific controls, and which must never be asked.

The bug this covers shipped: every RTSP camera showed a *Tapo controls* button,
including Hikvision NVR channels that share one host with a real Tapo
camera on the same subnet. Opening it produced a vendor error per channel and
taught the operator to ignore errors.

Two rules are asserted throughout. Sentinel offers the controls only when the
camera's own metadata identifies TP-Link, and it never infers a brand from an
address -- the NVR channels below share ``192.168.50.179`` with each other, and
sit on the same network as genuine Tapo hardware.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.cameras.vendor import TapoControlSource, classify_tapo_controls
from app.database.models import Camera, CameraKind

# A typical mixed setup: one NVR answering several channels, a few Tapo cameras
# (one of which never reported a model) and a battery camera.
TAPO_MODEL = {
    "name": "Carport",
    "main_stream_url": "rtsp://192.168.50.217:554/stream1",
    "username": "camera-admin",
    "password": "camera-secret",
    "model": "TC60",
}
TAPO_MANUFACTURER = {
    "name": "Back door",
    "main_stream_url": "rtsp://192.168.50.185:554/stream1",
    "username": "camera-admin",
    "password": "camera-secret",
    "manufacturer": "TP-Link Technologies Co., Ltd.",
}
NVR_CHANNEL = {
    "name": "Courtyard",
    "main_stream_url": "rtsp://192.168.50.179:554/Streaming/Channels/101",
    "username": "admin",
    "password": "nvr-secret",
}
NVR_SIBLING = {
    "name": "Stairwell",
    "main_stream_url": "rtsp://192.168.50.179:554/Streaming/Channels/301",
    "username": "admin",
    "password": "nvr-secret",
}
UNKNOWN_CAMERA = {
    "name": "Storeroom",
    "main_stream_url": "rtsp://192.168.50.118:554/live",
    "username": "camera-admin",
    "password": "camera-secret",
}


def _camera(**fields: Any) -> Camera:
    """A detached ORM object. Classification reads columns, not the database."""
    defaults: dict[str, Any] = {
        "id": "cam",
        "name": "Camera",
        "kind": CameraKind.RTSP,
        "host": "192.168.50.10",
        "manufacturer": None,
        "model": None,
        "main_stream_url": None,
        "sub_stream_url": None,
        "tapo_controls_enabled": None,
    }
    defaults.update(fields)
    return Camera(**defaults)


class TestPositiveIdentification:
    def test_a_tp_link_manufacturer_enables_the_controls(self) -> None:
        result = classify_tapo_controls(_camera(manufacturer="TP-Link Technologies Co., Ltd."))
        assert result.supported
        assert result.source is TapoControlSource.METADATA

    @pytest.mark.parametrize("spelling", ["TP-Link", "TP LINK", "tplink", "Tapo", "TP-Link Corp"])
    def test_the_manufacturer_match_survives_how_vendors_spell_themselves(
        self, spelling: str
    ) -> None:
        assert classify_tapo_controls(_camera(manufacturer=spelling)).supported

    @pytest.mark.parametrize(
        "model", ["TC60", "TC82", "C210", "C320WS", "Tapo C200", "D230S1", "H200", "VIGI C340"]
    )
    def test_a_tapo_model_number_enables_the_controls(self, model: str) -> None:
        result = classify_tapo_controls(_camera(model=model))
        assert result.supported, model
        assert result.source is TapoControlSource.METADATA

    def test_a_battery_camera_is_always_tapo(self) -> None:
        result = classify_tapo_controls(_camera(kind=CameraKind.TAPO_BATTERY, host="192.168.50.220"))
        assert result.supported
        assert result.source is TapoControlSource.KIND


class TestNegativeIdentification:
    @pytest.mark.parametrize(
        "manufacturer", ["Hikvision", "HIKVISION Digital Technology", "Dahua", "Reolink", "Axis"]
    )
    def test_a_known_other_manufacturer_hides_the_controls(self, manufacturer: str) -> None:
        result = classify_tapo_controls(_camera(manufacturer=manufacturer))
        assert not result.supported
        assert result.source is TapoControlSource.OTHER_VENDOR
        assert not result.can_opt_in, "a positively identified other vendor is not a maybe"

    @pytest.mark.parametrize(
        "url",
        [
            "rtsp://192.168.50.179:554/Streaming/Channels/101",
            "rtsp://192.168.50.179:554/Streaming/Channels/401",
            "rtsp://192.168.50.50:554/cam/realmonitor?channel=1&subtype=0",
            "rtsp://192.168.50.51:554/h264Preview_01_main",
            "rtsp://192.168.50.52:554/axis-media/media.amp",
        ],
    )
    def test_a_vendor_specific_stream_layout_hides_the_controls(self, url: str) -> None:
        result = classify_tapo_controls(_camera(main_stream_url=url))
        assert not result.supported
        assert result.source is TapoControlSource.OTHER_VENDOR

    def test_a_hikvision_model_number_is_not_read_as_a_tapo_d_series(self) -> None:
        result = classify_tapo_controls(_camera(model="DS-2CD2043G2-I"))
        assert not result.supported
        assert result.source is TapoControlSource.OTHER_VENDOR

    def test_a_stale_manual_opt_in_cannot_override_an_identified_other_vendor(self) -> None:
        result = classify_tapo_controls(
            _camera(
                manufacturer="Hikvision",
                model="DS-2CD2043G2-I",
                tapo_controls_enabled=True,
            )
        )
        assert not result.supported
        assert result.source is TapoControlSource.OTHER_VENDOR


class TestUnknownCameras:
    def test_a_camera_with_no_metadata_is_unknown_not_tapo(self) -> None:
        result = classify_tapo_controls(_camera(main_stream_url="rtsp://192.168.50.118:554/live"))
        assert not result.supported
        assert result.source is TapoControlSource.UNKNOWN
        assert result.can_opt_in

    def test_an_explicit_opt_in_enables_the_controls(self) -> None:
        result = classify_tapo_controls(
            _camera(main_stream_url="rtsp://192.168.50.118:554/live", tapo_controls_enabled=True)
        )
        assert result.supported
        assert result.source is TapoControlSource.MANUAL

    def test_an_explicit_opt_out_overrides_detection(self) -> None:
        """An operator who says no outranks the metadata, even for a real Tapo."""
        result = classify_tapo_controls(
            _camera(manufacturer="TP-Link", model="TC60", tapo_controls_enabled=False)
        )
        assert not result.supported
        assert result.source is TapoControlSource.MANUAL

    def test_the_address_alone_never_decides(self) -> None:
        """A common shape: one host, mixed brands."""
        tapo_camera = _camera(
            host="192.168.50.179",
            manufacturer="TP-Link",
            main_stream_url="rtsp://192.168.50.179:554/stream1",
        )
        nvr_channel = _camera(
            host="192.168.50.179",
            main_stream_url="rtsp://192.168.50.179:554/Streaming/Channels/201",
        )
        assert classify_tapo_controls(tapo_camera).supported
        assert not classify_tapo_controls(nvr_channel).supported


class TestApiSurface:
    async def test_every_camera_reports_why_it_did_or_did_not_get_the_controls(
        self, admin_client: AsyncClient
    ) -> None:
        for payload in (TAPO_MODEL, TAPO_MANUFACTURER, NVR_CHANNEL, NVR_SIBLING, UNKNOWN_CAMERA):
            created = await admin_client.post("/api/v1/cameras", json=payload)
            assert created.status_code == 201, created.text

        listing = (await admin_client.get("/api/v1/cameras")).json()
        by_name = {camera["name"]: camera for camera in listing}

        assert by_name["Carport"]["tapo_controls"]["supported"] is True
        assert by_name["Back door"]["tapo_controls"]["supported"] is True
        assert by_name["Courtyard"]["tapo_controls"]["supported"] is False
        assert by_name["Stairwell"]["tapo_controls"]["supported"] is False
        assert by_name["Storeroom"]["tapo_controls"]["supported"] is False
        assert by_name["Storeroom"]["tapo_controls"]["can_opt_in"] is True
        assert by_name["Courtyard"]["tapo_controls"]["can_opt_in"] is False
        for camera in by_name.values():
            assert camera["tapo_controls"]["reason"]

    async def test_the_tapo_routes_refuse_a_non_tapo_camera(
        self, admin_client: AsyncClient
    ) -> None:
        """Enforced server-side, not just hidden in the interface."""
        created = (await admin_client.post("/api/v1/cameras", json=NVR_CHANNEL)).json()
        for path in ("capabilities", "state"):
            response = await admin_client.get(f"/api/v1/cameras/{created['id']}/tapo/{path}")
            assert response.status_code == 409, response.text
            assert "Hikvision" in response.json()["detail"]

    async def test_a_known_other_vendor_rejects_manual_opt_in(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=NVR_CHANNEL)).json()
        response = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"tapo_controls_enabled": True}
        )
        assert response.status_code == 422, response.text
        assert "another vendor" in response.json()["detail"]

        reloaded = (await admin_client.get(f"/api/v1/cameras/{created['id']}")).json()
        assert reloaded["tapo_controls_enabled"] is None
        assert reloaded["tapo_controls"]["source"] == "other_vendor"

    async def test_an_unknown_camera_is_refused_until_an_administrator_opts_in(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=UNKNOWN_CAMERA)).json()
        refused = await admin_client.get(f"/api/v1/cameras/{created['id']}/tapo/capabilities")
        assert refused.status_code == 409

        updated = await admin_client.patch(
            f"/api/v1/cameras/{created['id']}", json={"tapo_controls_enabled": True}
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["tapo_controls"]["supported"] is True
        assert updated.json()["tapo_controls"]["source"] == "manual"

        # Now it is allowed through the gate and fails on its own merits --
        # there is no camera at that address in a test run.
        allowed = await admin_client.get(f"/api/v1/cameras/{created['id']}/tapo/capabilities")
        assert allowed.status_code != 409

    async def test_the_opt_in_does_not_apply_to_battery_cameras(
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
        rejected = await admin_client.patch(
            f"/api/v1/cameras/{created.json()['id']}", json={"tapo_controls_enabled": False}
        )
        assert rejected.status_code == 422
