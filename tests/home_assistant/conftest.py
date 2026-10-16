"""Shared fixtures.

The HTTP layer is faked with Home Assistant's own ``aioclient_mock``, which
intercepts ``async_get_clientsession`` -- so every line of the integration runs
for real (the client, its error mapping, the coordinator, every entity) and only
the wire is simulated. The alternative, mocking ``SentinelClient`` itself, would
prove nothing: the tests would agree with whatever the author believed the
server sends.

The payload shapes below are taken from the server's own contract document,
`backend/app/integrations/CONTRACT.md` in the sentinel-cctv repository, so a
drift between the two shows up here rather than on somebody's Home Assistant.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.sentinel.const import CONF_TOKEN, CONF_URL, DOMAIN

TOKEN = "sctv_testtoken"
SERVER_ID = "sentinel-test-install"
BASE_URL = "http://192.0.2.10:8090"
API = f"{BASE_URL}/api/v1"

CAMERA_PAYLOAD: dict[str, Any] = {
    "id": "cam-1",
    "name": "Front door",
    "enabled": True,
    "status": "online",
    "manufacturer": "TP-Link",
    "model": "Tapo C310",
    "recording_mode": "continuous",
    "recording_state": "recording",
    "motion_enabled": True,
    "motion_active": False,
    "stream": {
        "codec": "h264",
        "width": 2560,
        "height": 1440,
        "fps": 15.0,
        "audio_codec": "aac",
        "bitrate_kbps": 4200,
    },
}

HANDSHAKE_PAYLOAD: dict[str, Any] = {
    "unique_id": SERVER_ID,
    "name": "Sentinel CCTV",
    "version": "0.1.0",
    "api_version": 1,
    "websocket_path": "/api/v1/events/stream",
    "cameras": [CAMERA_PAYLOAD],
}

STORAGE_PAYLOAD: dict[str, Any] = {
    "available": True,
    "total_bytes": 2_000_000_000_000,
    "used_bytes": 800_000_000_000,
    "free_bytes": 1_200_000_000_000,
    "recordings_bytes": 750_000_000_000,
    "segment_count": 42_000,
}

SNAPSHOT_BYTES = b"\xff\xd8\xff\xe0jpeg-bytes"


if sys.platform == "win32":  # pragma: no cover - CI runs on Linux
    pytest.skip(
        "The Home Assistant test harness does not run on Windows: aiodns refuses "
        "the default ProactorEventLoop, uvloop has no Windows build, and "
        "asyncio's self-pipe needs an AF_UNIX socketpair that does not exist "
        "there. Run this suite on Linux, which is what CI does.",
        allow_module_level=True,
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Home Assistant will not load a custom component in tests without this."""
    return


@pytest.fixture(autouse=True)
def no_websocket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the push connection out of the way.

    ``aioclient_mock`` does not speak WebSocket, and the event stream has its
    own dedicated tests that drive frames straight into the coordinator. Left
    running, its reconnect loop would spin against a mock that cannot answer
    and make every other test slow and noisy for no added coverage.
    """
    from custom_components.sentinel import coordinator as coordinator_module

    async def _noop(self: Any) -> None:
        return None

    monkeypatch.setattr(
        coordinator_module.SentinelDataUpdateCoordinator, "async_start_push", _noop
    )
    monkeypatch.setattr(
        coordinator_module.SentinelDataUpdateCoordinator, "async_stop_push", _noop
    )


def register_sentinel(
    mock: AiohttpClientMocker,
    *,
    handshake: dict[str, Any] | None = None,
    handshake_status: int = 200,
    cameras: list[dict[str, Any]] | None = None,
) -> None:
    """Answer every request the integration makes during a normal set-up."""
    mock.get(
        f"{API}/integrations/home-assistant",
        status=handshake_status,
        json=handshake if handshake is not None else HANDSHAKE_PAYLOAD,
    )
    mock.get(
        f"{API}/cameras", json=cameras if cameras is not None else [CAMERA_PAYLOAD]
    )
    mock.get(f"{API}/storage", json=STORAGE_PAYLOAD)
    mock.get(f"{API}/storage/settings", json={"retention_days": 14})
    mock.get(f"{API}/events", json={"events": [], "total": 7, "limit": 1, "offset": 0})
    mock.get(f"{API}/cameras/cam-1/snapshot", content=SNAPSHOT_BYTES)
    mock.patch(
        f"{API}/cameras/cam-1", json={"id": "cam-1", "recording_mode": "continuous"}
    )


@pytest.fixture
def sentinel_api(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """A Sentinel server that answers everything, ready to be overridden."""
    register_sentinel(aioclient_mock)
    return aioclient_mock


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Sentinel CCTV",
        unique_id=SERVER_ID,
        data={CONF_URL: BASE_URL, CONF_TOKEN: TOKEN},
    )


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant,
    sentinel_api: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> AsyncIterator[MockConfigEntry]:
    """A set-up integration, with every platform forwarded."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
