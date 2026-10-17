"""Shared test fixtures.

Every test gets its own data directory, its own SQLite file and its own install
secret, so nothing leaks between tests and none of them touch a real install.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.ratelimit import reset_all_limiters
from app.cameras.ptz import set_manager as set_ptz_manager
from app.cameras.tapo import set_manager as set_tapo_manager
from app.config import Settings, get_settings, reset_settings_cache
from app.database.session import dispose_engine
from app.logging_config import configure_logging
from app.motion.cleanup import set_manager as set_event_cleanup_manager
from app.motion.manager import set_manager as set_motion_manager
from app.recording.export import set_manager as set_export_manager
from app.recording.manager import set_manager as set_recording_manager
from app.reliability.monitor import set_monitor as set_reliability_monitor
from app.security.crypto import reset_key_cache
from app.storage.cleanup import set_manager as set_cleanup_manager
from app.streaming.manager import set_manager as set_streaming_manager
from app.system import health
from app.system.privacy import set_manager as set_privacy_manager

configure_logging("WARNING")

ADMIN_USERNAME = "operator"
ADMIN_PASSWORD = "correct-horse-battery"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def recordings_dir(tmp_path: Path) -> Path:
    path = tmp_path / "recordings"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def settings(data_dir: Path, recordings_dir: Path) -> Iterator[Settings]:
    previous = {
        key: os.environ.get(key)
        for key in ("SENTINEL_DATA_DIR", "SENTINEL_RECORDINGS_DIR", "SENTINEL_TESTING")
    }
    os.environ["SENTINEL_DATA_DIR"] = str(data_dir)
    os.environ["SENTINEL_RECORDINGS_DIR"] = str(recordings_dir)
    os.environ["SENTINEL_TESTING"] = "true"

    reset_settings_cache()
    reset_key_cache()
    current = get_settings()
    current.ensure_directories()
    yield current

    reset_settings_cache()
    reset_key_cache()
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    from app.main import create_app

    health.reset()
    await reset_all_limiters()
    # The managers are process-wide singletons; each test gets fresh ones bound
    # to that test's temporary settings and storage.
    set_streaming_manager(None)
    set_recording_manager(None)
    set_motion_manager(None)
    set_event_cleanup_manager(None)
    set_export_manager(None)
    set_ptz_manager(None)
    set_tapo_manager(None)
    set_privacy_manager(None)
    set_reliability_monitor(None)
    set_cleanup_manager(None)

    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application

    await dispose_engine()
    health.reset()
    set_streaming_manager(None)
    set_recording_manager(None)
    set_motion_manager(None)
    set_event_cleanup_manager(None)
    set_export_manager(None)
    set_ptz_manager(None)
    set_tapo_manager(None)
    set_privacy_manager(None)
    set_reliability_monitor(None)
    set_cleanup_manager(None)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://sentinel.test") as async_client:
        yield async_client


@pytest.fixture
async def admin_client(client: AsyncClient) -> AsyncClient:
    """A client already signed in as the first administrator."""
    response = await client.post(
        "/api/v1/auth/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 201, response.text

    response = await client.post(
        "/api/v1/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client
