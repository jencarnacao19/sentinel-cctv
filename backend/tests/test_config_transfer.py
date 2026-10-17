"""Moving an installation to another machine with one YAML file."""

from __future__ import annotations

import yaml
from httpx import AsyncClient
from sqlalchemy import select

from app.database.models import Camera
from app.database.session import session_scope
from app.security.crypto import try_decrypt_secret
from app.system import settings_store

CAMERA_PAYLOAD = {
    "name": "Front door",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
    "sub_stream_url": "rtsp://192.168.50.50:554/stream2",
    "username": "camera-admin",
    "password": "camera-secret",
    "motion_sensitivity": 65,
}
ZONES = [
    {
        "name": "Gate",
        "kind": "detect",
        "sensitivity": 80,
        "points": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]],
    },
    {"name": "Tree", "kind": "ignore", "points": [[0.6, 0.1], [0.9, 0.1], [0.9, 0.4]]},
]
PASSPHRASE = "correct horse battery"


async def _install(admin_client: AsyncClient) -> str:
    created = await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
    assert created.status_code == 201, created.text
    camera_id: str = created.json()["id"]
    saved = await admin_client.put(
        f"/api/v1/cameras/{camera_id}/motion-zones", json={"zones": ZONES}
    )
    assert saved.status_code == 200, saved.text
    return camera_id


async def _export(admin_client: AsyncClient, **body: object) -> str:
    response = await admin_client.post("/api/v1/system/config/export", json=body)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/yaml")
    assert "attachment" in response.headers["content-disposition"]
    return response.text


async def _import(
    admin_client: AsyncClient, text: str, passphrase: str | None = None
) -> tuple[int, dict[str, object]]:
    response = await admin_client.post(
        "/api/v1/system/config/import",
        files={"file": ("sentinel-config.yaml", text.encode(), "application/yaml")},
        data={"passphrase": passphrase} if passphrase else None,
    )
    return response.status_code, response.json()


async def _stored_camera(name: str) -> Camera | None:
    async with session_scope() as session:
        result = await session.execute(select(Camera).where(Camera.name == name))
        return result.scalar_one_or_none()


class TestExport:
    async def test_without_a_passphrase_no_password_is_in_the_file(
        self, admin_client: AsyncClient
    ) -> None:
        await _install(admin_client)
        text = await _export(admin_client)

        assert "camera-secret" not in text
        document = yaml.safe_load(text)
        assert document["format"] == "sentinel-config"
        assert document["passwords"] == "omitted"
        [camera] = document["cameras"]
        assert camera["name"] == "Front door"
        assert camera["main_stream_url"] == "rtsp://192.168.50.50:554/stream1"
        assert camera["username"] == "camera-admin"
        assert "password" not in camera
        assert camera["motion_sensitivity"] == 65
        assert [zone["name"] for zone in camera["motion_zones"]] == ["Gate", "Tree"]
        assert set(document["settings"]) == {"timezone", "recording", "motion", "storage"}

    async def test_with_a_passphrase_the_password_is_sealed_not_readable(
        self, admin_client: AsyncClient
    ) -> None:
        await _install(admin_client)
        text = await _export(admin_client, include_passwords=True, passphrase=PASSPHRASE)

        assert "camera-secret" not in text
        document = yaml.safe_load(text)
        assert document["passwords"] == "encrypted"
        assert document["encryption"]["kdf"] == "scrypt"
        assert set(document["cameras"][0]["password"]) == {"nonce", "ciphertext"}

    async def test_a_short_passphrase_is_refused(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/api/v1/system/config/export",
            json={"include_passwords": True, "passphrase": "short"},
        )
        assert response.status_code == 422

    async def test_including_passwords_needs_a_passphrase(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/api/v1/system/config/export", json={"include_passwords": True}
        )
        assert response.status_code == 422

    async def test_only_an_administrator_can_export(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/system/config/export", json={})).status_code == 401


class TestImport:
    async def test_a_new_machine_gets_cameras_zones_and_passwords_back(
        self, admin_client: AsyncClient
    ) -> None:
        camera_id = await _install(admin_client)
        text = await _export(admin_client, include_passwords=True, passphrase=PASSPHRASE)
        # The "new machine": the same camera list, emptied.
        assert (await admin_client.delete(f"/api/v1/cameras/{camera_id}")).status_code == 204

        status, summary = await _import(admin_client, text, PASSPHRASE)
        assert status == 200, summary
        assert summary["cameras_added"] == ["Front door"]
        assert summary["passwords_restored"] is True

        camera = await _stored_camera("Front door")
        assert camera is not None
        assert try_decrypt_secret(camera.encrypted_password) == "camera-secret"
        assert camera.motion_sensitivity == 65
        zones = (await admin_client.get(f"/api/v1/cameras/{camera.id}/motion-zones")).json()
        assert [(z["name"], z["kind"], z["sensitivity"]) for z in zones["zones"]] == [
            ("Gate", "detect", 80),
            ("Tree", "ignore", None),
        ]

    async def test_a_file_without_passwords_imports_cameras_without_them(
        self, admin_client: AsyncClient
    ) -> None:
        camera_id = await _install(admin_client)
        text = await _export(admin_client)
        await admin_client.delete(f"/api/v1/cameras/{camera_id}")

        status, summary = await _import(admin_client, text)
        assert status == 200, summary
        assert summary["passwords_restored"] is False
        camera = await _stored_camera("Front door")
        assert camera is not None
        assert camera.encrypted_password is None

    async def test_a_wrong_passphrase_changes_nothing(self, admin_client: AsyncClient) -> None:
        camera_id = await _install(admin_client)
        text = await _export(admin_client, include_passwords=True, passphrase=PASSPHRASE)
        await admin_client.delete(f"/api/v1/cameras/{camera_id}")

        status, summary = await _import(admin_client, text, "not the passphrase")
        assert status == 422
        assert "does not open" in str(summary["detail"])
        assert await _stored_camera("Front door") is None

    async def test_an_encrypted_file_asks_for_its_passphrase(
        self, admin_client: AsyncClient
    ) -> None:
        camera_id = await _install(admin_client)
        text = await _export(admin_client, include_passwords=True, passphrase=PASSPHRASE)
        await admin_client.delete(f"/api/v1/cameras/{camera_id}")

        status, summary = await _import(admin_client, text)
        assert status == 422
        assert "passphrase" in str(summary["detail"])

    async def test_a_camera_that_already_exists_is_kept_not_duplicated(
        self, admin_client: AsyncClient
    ) -> None:
        await _install(admin_client)
        text = await _export(admin_client)

        status, summary = await _import(admin_client, text)
        assert status == 200
        assert summary["cameras_added"] == []
        assert summary["cameras_skipped"] == [
            {"name": "Front door", "reason": "A camera with this name exists."}
        ]

    async def test_a_hand_edited_command_source_is_refused(self, admin_client: AsyncClient) -> None:
        """The import goes through the same URL checks as the API."""
        text = yaml.safe_dump(
            {
                "format": "sentinel-config",
                "version": 1,
                "passwords": "omitted",
                "cameras": [
                    {"name": "Evil", "kind": "rtsp", "main_stream_url": "exec:/bin/sh -c id"},
                    {"name": "Good", "kind": "rtsp", "main_stream_url": "rtsp://192.168.50.60/s"},
                ],
            }
        )
        status, summary = await _import(admin_client, text)
        assert status == 200, summary
        assert summary["cameras_added"] == ["Good"]
        assert [entry["name"] for entry in summary["cameras_skipped"]] == ["Evil"]  # type: ignore[union-attr]
        assert await _stored_camera("Evil") is None

    async def test_a_storage_policy_that_could_delete_footage_is_not_applied(
        self, admin_client: AsyncClient
    ) -> None:
        text = yaml.safe_dump(
            {
                "format": "sentinel-config",
                "version": 1,
                "cameras": [],
                "settings": {
                    "timezone": "Europe/Lisbon",
                    "storage": {"retention_days": 3},
                },
            }
        )
        status, summary = await _import(admin_client, text)
        assert status == 200, summary
        assert summary["settings_applied"] == ["timezone"]
        assert [entry["name"] for entry in summary["settings_skipped"]] == ["storage"]  # type: ignore[union-attr]
        async with session_scope() as session:
            assert await settings_store.get_setting(session, settings_store.TIMEZONE) == (
                "Europe/Lisbon"
            )
            assert (
                await settings_store.get_setting(session, settings_store.STORAGE_RETENTION_DAYS)
                == 14
            )

    async def test_a_looser_storage_policy_is_applied(self, admin_client: AsyncClient) -> None:
        text = yaml.safe_dump(
            {
                "format": "sentinel-config",
                "version": 1,
                "cameras": [],
                "settings": {"storage": {"retention_days": 60}},
            }
        )
        status, summary = await _import(admin_client, text)
        assert status == 200, summary
        assert summary["settings_applied"] == ["storage.retention_days"]

    async def test_a_file_that_is_not_an_export_is_refused(self, admin_client: AsyncClient) -> None:
        status, summary = await _import(admin_client, "just: some yaml\n")
        assert status == 422
        assert "not a Sentinel configuration" in str(summary["detail"])

    async def test_only_an_administrator_can_import(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/system/config/import",
            files={"file": ("x.yaml", b"format: sentinel-config\n", "application/yaml")},
        )
        assert response.status_code == 401
