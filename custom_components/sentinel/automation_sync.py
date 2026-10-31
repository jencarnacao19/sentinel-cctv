"""Writes Sentinel's automation recipes into Home Assistant's automations.yaml.

Runs only when the owner turned on "Let Sentinel create automations" in this
integration's options. It edits the same file, with the same helpers, as Home
Assistant's own automation editor, and touches only entries whose id starts
with ``sentinel_``. A hash of what it last wrote tells it when someone edited
one of those in Home Assistant; from then on that automation is left alone
until the administrator gives it back to Sentinel.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.yaml import dump, load_yaml

from .api import SentinelError
from .automations import Entities, RecipeError, automation_id, build_automation, refusal
from .const import CONF_ALLOW_AUTOMATIONS, DOMAIN
from .coordinator import SentinelDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
_LOCKS: dict[str, asyncio.Lock] = {}


def _hash(config: Any) -> str:
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, default=str).encode()
    ).hexdigest()


def _targets(hass: HomeAssistant, allowed: bool, has_file: bool) -> dict[str, Any]:
    def states(domain: str) -> list[dict[str, str]]:
        return [
            {"id": state.entity_id, "name": state.name}
            for state in sorted(hass.states.async_all(domain), key=lambda s: s.name)
        ]

    notify = sorted(hass.services.async_services().get("notify", {}))
    return {
        "allowed": allowed,
        "automations_file": has_file,
        "notify": [{"id": name, "name": name.replace("_", " ")} for name in notify],
        "lights": states("light"),
        "people": states("person"),
        "alarms": states("alarm_control_panel"),
        "reported_at": datetime.now(UTC).isoformat(),
    }


def _entities(
    hass: HomeAssistant,
    coordinator: SentinelDataUpdateCoordinator,
    recipe: dict[str, Any],
) -> Entities:
    registry = er.async_get(hass)
    server = coordinator.data.server.unique_id
    cameras = coordinator.data.cameras

    def find(domain: str, unique_id: str) -> str | None:
        return registry.async_get_entity_id(domain, DOMAIN, unique_id)

    camera_id = recipe.get("camera_id")
    zone_id = recipe.get("zone_id")
    camera = cameras.get(camera_id) if camera_id else None
    zone = camera.zone(zone_id) if camera and zone_id else None
    switches = {
        cid: switch
        for cid in cameras
        if (switch := find("switch", f"{server}_{cid}_recording")) is not None
    }
    return Entities(
        camera_name=camera.name if camera else "",
        zone_name=zone.name if zone else None,
        motion=(
            find("binary_sensor", f"{server}_{camera_id}_zone_{zone_id}")
            if zone
            else find("binary_sensor", f"{server}_{camera_id}_motion")
            if camera and not zone_id
            else None
        ),
        camera=find("camera", f"{server}_{camera_id}_camera") if camera else None,
        recording_switches=tuple(
            switches[cid] for cid in recipe.get("camera_ids") or [] if cid in switches
        ),
        recording_sensors=tuple(
            sensor
            for cid in cameras
            if (sensor := find("sensor", f"{server}_{cid}_recording_state")) is not None
        ),
        storage_free=find("sensor", f"{server}_storage_free"),
        own_switches=frozenset(switches.values()),
    )


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = load_yaml(str(path))
    return list(data) if isinstance(data, list) else []


def _write(path: Path, automations: list[dict[str, Any]]) -> None:
    # One copy of the file as it was before Sentinel ever wrote to it.
    backup = path.with_name(f"{path.name}.before-sentinel")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    write_utf8_file_atomic(str(path), dump(automations))


async def async_sync_automations(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SentinelDataUpdateCoordinator
) -> None:
    """Report targets, then bring automations.yaml in line with the recipes."""
    lock = _LOCKS.setdefault(entry.entry_id, asyncio.Lock())
    async with lock:
        try:
            await _sync(hass, entry, coordinator)
        except SentinelError as err:
            _LOGGER.debug("Sentinel automations not synced: %s", err)


async def _sync(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SentinelDataUpdateCoordinator
) -> None:
    client = coordinator.client
    path = Path(hass.config.path(AUTOMATION_CONFIG_PATH))
    allowed = bool(entry.options.get(CONF_ALLOW_AUTOMATIONS, False))
    has_file = await hass.async_add_executor_job(path.exists)
    await client.async_put_targets(_targets(hass, allowed, has_file))

    recipes: list[dict[str, Any]] = (await client.async_get_automations()).get(
        "recipes", []
    )
    if not allowed or not has_file:
        state = "waiting_permission" if not allowed else "unsupported"
        message = (
            "Turn on “Let Sentinel create automations” in the integration's options."
            if not allowed
            else "This Home Assistant does not keep its automations in automations.yaml."
        )
        await client.async_put_automation_status(
            {recipe["id"]: {"state": state, "message": message} for recipe in recipes}
        )
        return

    store: Store[dict[str, Any]] = Store(
        hass, 1, f"{DOMAIN}.automations.{entry.entry_id}"
    )
    written: dict[str, Any] = await store.async_load() or {}
    automations = await hass.async_add_executor_job(_read, path)
    by_id = {item.get("id"): index for index, item in enumerate(automations)}
    status: dict[str, dict[str, str]] = {}
    changed = False

    for recipe in recipes:
        own_id = automation_id(recipe["id"])
        existing = automations[by_id[own_id]] if own_id in by_id else None
        previous = written.get(recipe["id"])
        edited = (
            existing is not None
            and previous is not None
            and _hash(existing) != previous["hash"]
        )
        if edited and recipe.get("reclaim", 0) <= previous.get("reclaim", 0):
            status[recipe["id"]] = {"state": "edited_in_ha", "message": ""}
            continue
        entities = _entities(hass, coordinator, recipe)
        try:
            config = build_automation(recipe, entities)
        except RecipeError as err:
            status[recipe["id"]] = {"state": "refused", "message": str(err)}
            continue
        reason = refusal(config, entities.own_switches)
        if reason:
            status[recipe["id"]] = {"state": "refused", "message": reason}
            continue
        if existing != config:
            if existing is None:
                automations.append(config)
                by_id[own_id] = len(automations) - 1
            else:
                automations[by_id[own_id]] = config
            changed = True
        written[recipe["id"]] = {
            "hash": _hash(config),
            "reclaim": recipe.get("reclaim", 0),
        }
        status[recipe["id"]] = {"state": "created", "message": ""}

    # A recipe the administrator deleted takes its automation with it, unless
    # someone made that automation their own in Home Assistant.
    kept = {recipe["id"] for recipe in recipes}
    for recipe_id in [rid for rid in written if rid not in kept]:
        own_id = automation_id(recipe_id)
        if (
            own_id in by_id
            and _hash(automations[by_id[own_id]]) == written[recipe_id]["hash"]
        ):
            automations = [item for item in automations if item.get("id") != own_id]
            by_id = {item.get("id"): index for index, item in enumerate(automations)}
            changed = True
        written.pop(recipe_id)

    if changed:
        await hass.async_add_executor_job(_write, path, automations)
        if hass.services.has_service("automation", "reload"):
            await hass.services.async_call("automation", "reload", blocking=True)
    await store.async_save(written)
    await client.async_put_automation_status(status)
