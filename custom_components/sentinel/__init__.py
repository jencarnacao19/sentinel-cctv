"""Sentinel CCTV integration: set up and tear down one config entry.

Everything platform-specific hangs off the coordinator, so this module is
short and stays that way: adding a fifth platform, five phases from now, should
not have to touch it beyond one more entry in ``PLATFORMS``.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SentinelClient
from .const import (
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import SentinelDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.SENSOR,
    Platform.SWITCH,
]

type SentinelConfigEntry = ConfigEntry[SentinelDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SentinelConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = SentinelClient(session, entry.data[CONF_URL], entry.data[CONF_TOKEN])
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS)
    coordinator = SentinelDataUpdateCoordinator(
        hass, entry, client, scan_interval_seconds=scan_interval
    )

    # Raises ConfigEntryNotReady (server unreachable) or ConfigEntryAuthFailed
    # (token rejected, which starts Home Assistant's reauth flow) on failure;
    # neither needs to be caught here, both are exactly the right outcome.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await coordinator.async_start_push()

    # Create the hub before forwarding camera platforms. Platform setup runs
    # concurrently, so relying on the first system sensor to create it can make
    # a camera reference a via_device that does not exist yet. Home Assistant
    # warns about that today and rejects it in newer releases.
    server = coordinator.data.server
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, server.unique_id)},
        name=server.name or "Sentinel CCTV",
        manufacturer=MANUFACTURER,
        model="Sentinel NVR",
        sw_version=server.version,
        configuration_url=coordinator.client.base_url,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options_change))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SentinelConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop_push()
    return unload_ok


async def _async_reload_on_options_change(
    hass: HomeAssistant, entry: SentinelConfigEntry
) -> None:
    """The polling-interval option only takes effect through a fresh coordinator."""
    await hass.config_entries.async_reload(entry.entry_id)
