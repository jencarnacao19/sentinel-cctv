"""Integration permissions, HA compatibility and migration of existing credentials."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from alembic import command
from app.cameras import ptz
from app.cameras import router as camera_router
from app.cameras.schemas import CameraTestResponse, StreamTestResponse
from app.config import Settings
from app.database.bootstrap import build_alembic_config


async def use_token(client: AsyncClient, scope: str) -> dict:
    response = await client.post("/api/v1/auth/tokens", json={"name": "HA", "scope": scope})
    assert response.status_code == 201, response.text
    token = response.json()
    client.cookies.clear()
    client.headers["Authorization"] = f"Bearer {token['token']}"
    return token


@pytest.mark.parametrize("scope", ["read_only", "camera_control"])
async def test_tokens_cannot_administer_or_delete_footage(
    admin_client: AsyncClient, scope: str
) -> None:
    token = await use_token(admin_client, scope)
    whoami = (await admin_client.get("/api/v1/auth/whoami")).json()
    assert whoami["is_admin"] is False
    assert whoami["scope"] == scope
    denied = [
        ("GET", "/auth/tokens"),
        ("POST", "/auth/tokens"),
        ("DELETE", f"/auth/tokens/{token['id']}"),
        ("GET", "/auth/login-security"),
        ("PATCH", "/auth/login-security"),
        ("GET", "/auth/login-blocks"),
        ("DELETE", "/auth/login-blocks/203.0.113.1"),
        ("POST", "/auth/login-blocks/clear"),
        ("GET", "/system/diagnostics"),
        ("GET", "/system/diagnostics/download"),
        ("POST", "/system/backup"),
        ("POST", "/system/backup/inspect"),
        ("POST", "/system/backup/restore"),
        ("PATCH", "/storage/settings"),
        ("POST", "/storage/sweep"),
        ("POST", "/storage/cleanup"),
        ("POST", "/storage/cleanup/preview"),
        ("DELETE", "/storage/cleanup"),
        ("POST", "/events/cleanup"),
        ("POST", "/events/cleanup/preview"),
        ("DELETE", "/events/cleanup"),
        ("PATCH", "/events/missing"),
        ("PATCH", "/recordings/missing"),
        ("POST", "/exports"),
        ("PATCH", "/exports/missing"),
        ("DELETE", "/exports/missing"),
        ("POST", "/cameras"),
        ("DELETE", "/cameras/missing"),
        ("POST", "/cameras/test-stream"),
        ("PUT", "/cameras/missing/motion-zones"),
        ("POST", "/cameras/missing/tapo/reboot"),
        ("PUT", "/cameras/tapo-battery/timed-recording/max-minutes"),
    ]
    for method, path in denied:
        response = await admin_client.request(method, f"/api/v1{path}", json={})
        assert response.status_code == 403, (method, path, response.status_code, response.text)


async def test_read_only_token_cannot_operate_cameras(admin_client: AsyncClient) -> None:
    await use_token(admin_client, "read_only")
    denied = [
        ("PATCH", "/cameras/missing"),
        ("PUT", "/cameras/missing/recording"),
        ("POST", "/cameras/missing/test"),
        ("POST", "/cameras/missing/ptz/move"),
        ("POST", "/cameras/missing/ptz/stop"),
        ("POST", "/cameras/missing/ptz/presets/one/goto"),
        ("POST", "/cameras/missing/tapo/privacy"),
        ("POST", "/cameras/missing/tapo/siren/start"),
        ("POST", "/cameras/missing/tapo/siren/stop"),
        ("POST", "/cameras/missing/tapo-battery/timed-recording"),
        ("DELETE", "/cameras/missing/tapo-battery/timed-recording"),
        ("PUT", "/system/privacy"),
    ]
    for method, path in denied:
        response = await admin_client.request(method, f"/api/v1{path}", json={})
        assert response.status_code == 403, (method, path, response.text)


@pytest.mark.parametrize("scope", ["read_only", "camera_control"])
async def test_ha_telemetry_and_camera_reads_remain_available(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    monkeypatch.setattr(camera_router, "_sync_streams", AsyncMock())
    camera = (
        await admin_client.post(
            "/api/v1/cameras", json={"name": "Lobby", "main_stream_url": "rtsp://192.0.2.10/main"}
        )
    ).json()
    monkeypatch.setattr(camera_router, "_snapshot_via_go2rtc", AsyncMock(return_value=b"jpeg"))
    await use_token(admin_client, scope)
    for path in (
        "/integrations/home-assistant",
        "/system",
        "/system/health",
        "/system/privacy",
        "/storage",
        "/storage/settings",
        "/cameras",
        f"/cameras/{camera['id']}",
        f"/streams/{camera['id']}",
        f"/events?camera_id={camera['id']}&limit=1&offset=0",
        "/recordings",
        "/exports",
    ):
        response = await admin_client.get(f"/api/v1{path}")
        assert response.status_code == 200, (path, response.text)
    response = await admin_client.get(f"/api/v1/cameras/{camera['id']}/snapshot")
    assert response.status_code == 200
    assert response.content == b"jpeg"


async def test_camera_control_preserves_ha_switch_and_manual_controls(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(camera_router, "_sync_streams", AsyncMock())
    camera = (
        await admin_client.post(
            "/api/v1/cameras", json={"name": "Lobby", "main_stream_url": "rtsp://192.0.2.10/main"}
        )
    ).json()
    base = f"/api/v1/cameras/{camera['id']}"
    move = AsyncMock()
    stop = AsyncMock()
    goto = AsyncMock()
    monkeypatch.setattr(ptz.get_manager(), "move", move)
    monkeypatch.setattr(ptz.get_manager(), "stop_camera", stop)
    monkeypatch.setattr(ptz.get_manager(), "goto_preset", goto)
    probe = AsyncMock(return_value=CameraTestResponse(main=StreamTestResponse(ok=True)))
    monkeypatch.setattr(camera_router.service, "test_camera", probe)
    await use_token(admin_client, "camera_control")

    response = await admin_client.patch(base, json={"recording_mode": "off"})
    assert response.status_code == 200, response.text
    assert response.json()["recording_mode"] == "off"
    for payload in (
        {"recording_mode": "continuous", "name": "unauthorised"},
        {"password": "replacement"},
        {"host": "192.0.2.20"},
        {"retention_days": 1},
        {"motion_enabled": False},
        {"tapo_password": None},
        {"recording_mode": "off", "main_stream_url": None},
    ):
        denied = await admin_client.patch(base, json=payload)
        assert denied.status_code == 403, (payload, denied.text)
    # Unknown fields are rejected by the existing strict camera schema first.
    assert (await admin_client.patch(base, json={"recording_enabled": False})).status_code == 422
    assert (await admin_client.get(base)).json()["name"] == "Lobby"
    response = await admin_client.put(f"{base}/recording", json={"enabled": False, "confirm": True})
    assert response.status_code == 200, response.text
    assert response.json()["recording_enabled"] is False
    assert (await admin_client.post(f"{base}/test")).status_code == 200
    probe.assert_awaited_once()
    assert (await admin_client.post(f"{base}/ptz/move", json={"pan": 0.5})).status_code == 204
    assert (await admin_client.post(f"{base}/ptz/stop")).status_code == 204
    assert (await admin_client.post(f"{base}/ptz/presets/one/goto")).status_code == 204
    move.assert_awaited_once()
    stop.assert_awaited_once()
    goto.assert_awaited_once()
    response = await admin_client.put(
        "/api/v1/system/privacy", json={"enabled": True, "reason": "privacy"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True


async def test_token_creation_defaults_to_read_only_and_rejects_admin_scope(
    admin_client: AsyncClient,
) -> None:
    created = await admin_client.post("/api/v1/auth/tokens", json={"name": "Reader"})
    assert created.status_code == 201
    assert created.json()["scope"] == "read_only"
    for payload in ({"scope": "admin"}, {"scope": ""}, {"is_admin": True}):
        denied = await admin_client.post("/api/v1/auth/tokens", json={"name": "No", **payload})
        assert denied.status_code == 422
    rows = (await admin_client.get("/api/v1/auth/tokens")).json()
    assert rows[0]["scope"] == "read_only"
    assert "token" not in rows[0]
    assert "token_hash" not in rows[0]


def test_migration_preserves_existing_tokens_with_control_and_defaults_new_to_read_only(
    settings: Settings,
) -> None:
    config = build_alembic_config(settings)
    command.upgrade(config, "0017_login_ip_blocking")
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO api_tokens (id, name, prefix, token_hash, created_at, revoked_at) "
            "VALUES ('existing', 'HA', 'sctv_test', 'existing-hash', '2026-09-01', NULL)"
        )
        connection.execute(
            "INSERT INTO api_tokens "
            "(id, name, prefix, token_hash, created_at, expires_at, revoked_at) "
            "VALUES ('retired', 'Old HA', 'sctv_old', 'old-hash', '2026-08-01', "
            "'2026-09-01', '2026-08-15')"
        )
    command.upgrade(config, "head")
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute(
            "SELECT scope, token_hash, revoked_at FROM api_tokens WHERE id = 'existing'"
        ).fetchone() == ("camera_control", "existing-hash", None)
        assert connection.execute(
            "SELECT scope, token_hash, expires_at, revoked_at FROM api_tokens WHERE id = 'retired'"
        ).fetchone() == ("camera_control", "old-hash", "2026-09-01", "2026-08-15")
        connection.execute(
            "INSERT INTO api_tokens (id, name, prefix, token_hash, created_at) "
            "VALUES ('new', 'Reader', 'sctv_new', 'new-hash', '2026-09-12')"
        )
        assert connection.execute("SELECT scope FROM api_tokens WHERE id = 'new'").fetchone() == (
            "read_only",
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE api_tokens SET scope = 'admin' WHERE id = 'existing'")
    command.downgrade(config, "0017_login_ip_blocking")
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_tokens").fetchone() == (3,)
        assert "scope" not in [
            row[1] for row in connection.execute("PRAGMA table_info(api_tokens)")
        ]
