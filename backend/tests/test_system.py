"""Health endpoint, system info and schema migrations."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect

from app.config import Settings
from app.database.bootstrap import current_revision, upgrade_to_head
from app.system import health


class TestHealth:
    async def test_health_is_unauthenticated_and_healthy(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/system/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["database"] is True
        assert body["storage"] is True

    async def test_documented_keys_are_always_present(self, client: AsyncClient) -> None:
        body = (await client.get("/api/v1/system/health")).json()
        for key in ("status", "database", "storage", "go2rtc", "recording"):
            assert key in body

    async def test_failing_critical_component_returns_503(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        del app
        health.register(
            "database",
            lambda: health.ComponentHealth(
                state=health.ComponentState.FAILED, detail="simulated outage"
            ),
            critical=True,
        )
        response = await client.get("/api/v1/system/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["database"] is False
        assert body["components"]["database"]["detail"] == "simulated outage"

    async def test_skipped_component_still_counts_as_healthy(self, client: AsyncClient) -> None:
        health.register(
            "go2rtc",
            lambda: health.ComponentHealth(
                state=health.ComponentState.SKIPPED, detail="No cameras configured yet."
            ),
            critical=False,
        )
        body = (await client.get("/api/v1/system/health")).json()
        assert body["status"] == "healthy"
        assert body["go2rtc"] is True

    async def test_probe_exception_is_contained(self, client: AsyncClient) -> None:
        def broken() -> bool:
            raise RuntimeError("probe blew up")

        health.register("recording", broken, critical=False)
        response = await client.get("/api/v1/system/health")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert response.json()["recording"] is False


class TestSystemInfo:
    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/system")).status_code == 401

    async def test_reports_version_and_setup_state(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/v1/system")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Sentinel CCTV"
        assert body["version"]
        assert body["setup_completed"] is False
        assert body["uptime_seconds"] >= 0


class TestRouting:
    """The SPA catch-all must not swallow API paths.

    These hold whether or not a built frontend is present: without one FastAPI
    returns the 404 itself, with one the fallback has to produce the same answer.
    """

    async def test_unknown_api_path_is_a_json_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/does-not-exist")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"detail": "Not found."}

    async def test_unknown_api_version_is_a_json_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/v9/cameras")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")

    async def test_traversal_in_a_static_path_is_refused(self, client: AsyncClient) -> None:
        response = await client.get("/../../etc/passwd")
        assert response.status_code in (307, 404) or "passwd" not in response.text

    async def test_browser_security_headers_allow_only_configured_frames(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        settings.frame_ancestors = [
            "https://ha.example.com",
            "http://192.168.1.205:8123",
            "http://homeassistant.local:8123",
        ]
        response = await client.get("/api/v1/system/health")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["permissions-policy"] == (
            "camera=(), microphone=(), geolocation=()"
        )
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "object-src 'none'" in csp
        assert "base-uri 'none'" in csp
        assert "frame-ancestors 'self' https://ha.example.com http://192.168.1.205:8123" in csp
        assert "http://homeassistant.local:8123" in csp
        assert "*" not in csp
        assert "'unsafe-eval'" not in csp
        assert response.headers["x-permitted-cross-domain-policies"] == "none"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert "x-frame-options" not in response.headers

    def test_frame_ancestors_allow_only_private_plain_http(self) -> None:
        settings = Settings(
            _env_file=None,
            frame_ancestors="https://ha.example.net,http://192.168.1.205:8123,http://homeassistant.local:8123",
        )
        assert settings.frame_ancestors == [
            "https://ha.example.net",
            "http://192.168.1.205:8123",
            "http://homeassistant.local:8123",
        ]

        with pytest.raises(ValidationError):
            Settings(_env_file=None, frame_ancestors="http://ha.example.net:8123")

    async def test_hsts_is_only_sent_for_https(self, client: AsyncClient) -> None:
        plain = await client.get("/api/v1/system/health")
        assert "strict-transport-security" not in plain.headers

        proxied_https = await client.get(
            "/api/v1/system/health", headers={"x-forwarded-proto": "https"}
        )
        assert proxied_https.headers["strict-transport-security"] == "max-age=31536000"


class TestMigrations:
    def test_upgrade_creates_expected_tables(self, settings: Settings) -> None:
        upgrade_to_head(settings)
        engine = create_engine(settings.sync_database_url)
        try:
            tables = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        assert {
            "users",
            "api_tokens",
            "app_settings",
            "login_client_states",
            "alembic_version",
        } <= tables

    def test_upgrade_is_idempotent(self, settings: Settings) -> None:
        upgrade_to_head(settings)
        first = current_revision(settings)
        upgrade_to_head(settings)
        assert current_revision(settings) == first
        assert first is not None

    def test_existing_data_survives_a_second_upgrade(self, settings: Settings) -> None:
        from sqlalchemy import text

        upgrade_to_head(settings)
        engine = create_engine(settings.sync_database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO app_settings (key, value, updated_at) "
                        "VALUES ('probe', '1', '2026-01-01 00:00:00')"
                    )
                )
        finally:
            engine.dispose()

        upgrade_to_head(settings)

        engine = create_engine(settings.sync_database_url)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text("SELECT value FROM app_settings WHERE key = 'probe'")
                ).scalar_one()
        finally:
            engine.dispose()
        assert row == "1"

    def test_no_revision_before_first_upgrade(self, settings: Settings) -> None:
        assert current_revision(settings) is None

    def test_database_file_lands_in_the_data_directory(
        self, settings: Settings, data_dir: Path
    ) -> None:
        upgrade_to_head(settings)
        assert settings.database_path.exists()
        assert data_dir in settings.database_path.parents
