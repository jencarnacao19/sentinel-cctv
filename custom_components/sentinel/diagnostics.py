"""Downloadable diagnostics, with credentials redacted.

A Home Assistant diagnostics bundle gets pasted into a public GitHub issue as a
matter of routine, so this is not an optional pass -- it is the one place an API
token would otherwise leak wholesale, since the config entry's own ``data``
carries it verbatim.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN, CONF_URL
from .redaction import redact_data

type SentinelConfigEntry = (
    Any  # avoided importing __init__ to keep this module standalone
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SentinelConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data

    return {
        "entry": {
            "data": redact_data(
                {
                    CONF_URL: entry.data.get(CONF_URL),
                    CONF_TOKEN: entry.data.get(CONF_TOKEN),
                }
            ),
            "options": dict(entry.options),
        },
        "push_connected": coordinator.push_connected,
        "last_update_success": coordinator.last_update_success,
        "server": redact_data(asdict(data.server)),
        "storage": redact_data(asdict(data.storage)),
        "retention_days": data.retention_days,
        "cameras": [redact_data(asdict(camera)) for camera in data.cameras.values()],
        "motion_active": dict(data.motion_active),
        "event_counts": dict(data.event_counts),
    }
