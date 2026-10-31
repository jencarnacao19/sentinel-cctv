"""Sentinel's automation recipes, as Home Assistant automation configs.

Pure functions: no Home Assistant object is touched here, so every rule about
what an automation may do is testable on its own. ``automation_sync`` resolves
the entities, calls these and writes the result.

The allowlist is the safety boundary the owner agreed to: notifications,
lights, Sentinel's own recording switches, and triggering an alarm panel only
while it is already armed. A recipe that would need anything else is refused,
whatever Sentinel sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DESCRIPTION = (
    "Created by Sentinel CCTV from its administration. Edit it here and Sentinel "
    "stops changing it."
)
ARMED_STATES = (
    "armed_away",
    "armed_home",
    "armed_night",
    "armed_vacation",
    "armed_custom_bypass",
)
#: Sentinel's recording state sensor reads this when a camera stopped recording.
NOT_RECORDING = "unavailable"


class RecipeError(ValueError):
    """The recipe cannot become an automation. The message says why."""


@dataclass(frozen=True)
class Entities:
    """The Home Assistant entities a recipe refers to, already resolved."""

    camera_name: str = ""
    zone_name: str | None = None
    #: The motion sensor of the chosen zone, or of the whole camera.
    motion: str | None = None
    camera: str | None = None
    recording_switches: tuple[str, ...] = ()
    recording_sensors: tuple[str, ...] = ()
    storage_free: str | None = None
    own_switches: frozenset[str] = field(default_factory=frozenset)


def automation_id(recipe_id: str) -> str:
    return f"sentinel_{recipe_id}"


def _when(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    when = recipe.get("when", "always")
    if when == "night":
        return [{"condition": "sun", "after": "sunset", "before": "sunrise"}]
    if when == "away":
        return [{"condition": "state", "entity_id": "zone.home", "state": "0"}]
    if when == "alarm_armed":
        return [_armed(recipe)]
    return []


def _armed(recipe: dict[str, Any]) -> dict[str, Any]:
    alarm = recipe.get("alarm_entity")
    if not alarm:
        raise RecipeError("Choose the alarm panel.")
    return {"condition": "state", "entity_id": alarm, "state": list(ARMED_STATES)}


def _notify(
    recipe: dict[str, Any], message: str, image: str | None
) -> list[dict[str, Any]]:
    services = recipe.get("notify") or []
    if not services:
        raise RecipeError("Choose at least one phone to notify.")
    data: dict[str, Any] = {
        "title": "Sentinel",
        "message": recipe.get("message") or message,
    }
    if image:
        data["data"] = {"image": f"/api/camera_proxy/{image}"}
    return [{"action": f"notify.{service}", "data": data} for service in services]


def _cooldown(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    minutes = recipe.get("cooldown_minutes", 5)
    return [{"delay": {"minutes": minutes}}] if minutes else []


def _motion_trigger(entities: Entities) -> list[dict[str, Any]]:
    if not entities.motion:
        raise RecipeError("The camera or motion zone no longer exists in Sentinel.")
    return [{"trigger": "state", "entity_id": entities.motion, "to": "on"}]


def build_automation(recipe: dict[str, Any], entities: Entities) -> dict[str, Any]:
    """The Home Assistant automation for one recipe. Raises RecipeError."""
    kind = recipe.get("kind")
    config: dict[str, Any] = {
        "id": automation_id(recipe["id"]),
        "alias": f"Sentinel: {recipe['name']}",
        "description": DESCRIPTION,
    }
    where = (
        f"{entities.zone_name} ({entities.camera_name})"
        if entities.zone_name
        else entities.camera_name
    )
    if kind == "motion_alert":
        image = entities.camera if recipe.get("image", True) else None
        config |= {
            # single + a closing delay is the cooldown: a new trigger is
            # dropped while the previous notification's delay runs.
            "mode": "single",
            "triggers": _motion_trigger(entities),
            "conditions": _when(recipe),
            "actions": _notify(recipe, f"Motion: {where}", image) + _cooldown(recipe),
        }
    elif kind == "system_alert":
        triggers: list[dict[str, Any]] = []
        if entities.recording_sensors:
            triggers.append(
                {
                    "trigger": "state",
                    "entity_id": list(entities.recording_sensors),
                    "to": NOT_RECORDING,
                    "for": {"minutes": 5},
                }
            )
        if entities.storage_free:
            triggers.append(
                {
                    "trigger": "numeric_state",
                    "entity_id": entities.storage_free,
                    "below": recipe.get("storage_below_gb", 20) * 1_000_000_000,
                }
            )
        if not triggers:
            raise RecipeError("Sentinel has no camera or storage sensor to watch yet.")
        config |= {
            "mode": "single",
            "triggers": triggers,
            "conditions": [],
            "actions": _notify(
                recipe,
                "Sentinel needs attention: {{ trigger.to_state.name }} is "
                "{{ trigger.to_state.state }}",
                None,
            )
            + _cooldown(recipe),
        }
    elif kind == "motion_actions":
        lights = list(recipe.get("lights") or [])
        actions: list[dict[str, Any]] = []
        if lights:
            actions.append({"action": "light.turn_on", "target": {"entity_id": lights}})
        if recipe.get("trigger_alarm"):
            alarm = _armed(recipe)
            actions.append(
                {
                    "if": [alarm],
                    "then": [
                        {
                            "action": "alarm_control_panel.alarm_trigger",
                            "target": {"entity_id": alarm["entity_id"]},
                        }
                    ],
                }
            )
        if lights:
            actions += [
                {"delay": {"minutes": recipe.get("minutes", 5)}},
                {"action": "light.turn_off", "target": {"entity_id": lights}},
            ]
        if not actions:
            raise RecipeError("Choose lights to turn on or the alarm to trigger.")
        config |= {
            # restart: more movement keeps the lights on for another period.
            "mode": "restart",
            "triggers": _motion_trigger(entities),
            "conditions": _when(recipe),
            "actions": actions,
        }
    elif kind == "presence_recording":
        switches = list(entities.recording_switches)
        if not switches:
            raise RecipeError("Choose the cameras to record while nobody is home.")
        config |= {
            "mode": "restart",
            "triggers": [{"trigger": "state", "entity_id": "zone.home"}],
            "conditions": [],
            "actions": [
                {
                    "choose": [
                        {
                            "conditions": [
                                {
                                    "condition": "state",
                                    "entity_id": "zone.home",
                                    "state": "0",
                                }
                            ],
                            "sequence": [
                                {
                                    "action": "switch.turn_on",
                                    "target": {"entity_id": switches},
                                }
                            ],
                        }
                    ],
                    "default": [
                        {"action": "switch.turn_off", "target": {"entity_id": switches}}
                    ],
                }
            ],
        }
    else:
        raise RecipeError(
            f"This version of the integration does not know {kind!r} recipes."
        )
    return config


def _targets(step: dict[str, Any]) -> list[str]:
    entity_id = (step.get("target") or {}).get("entity_id", [])
    return [entity_id] if isinstance(entity_id, str) else list(entity_id)


def _armed_only(condition: dict[str, Any]) -> bool:
    states = condition.get("state")
    states = [states] if isinstance(states, str) else states
    return bool(states) and set(states) <= set(ARMED_STATES)


def _check(
    steps: list[Any], own_switches: frozenset[str], armed: str | None
) -> str | None:
    for step in steps:
        if not isinstance(step, dict):
            return "An automation step has an unexpected shape."
        action = step.get("action")
        if action is None:
            if "delay" in step:
                continue
            if "if" in step:
                guard = next(
                    (
                        c.get("entity_id")
                        for c in step.get("if") or []
                        if isinstance(c, dict)
                        and c.get("condition") == "state"
                        and _armed_only(c)
                    ),
                    None,
                )
                reason = _check(step.get("then") or [], own_switches, guard) or _check(
                    step.get("else") or [], own_switches, None
                )
                if reason:
                    return reason
                continue
            if "choose" in step:
                for option in step["choose"]:
                    reason = _check(option.get("sequence") or [], own_switches, None)
                    if reason:
                        return reason
                reason = _check(step.get("default") or [], own_switches, None)
                if reason:
                    return reason
                continue
            return "An automation step does something Sentinel may not do."
        targets = _targets(step)
        if isinstance(action, str) and action.startswith("notify."):
            continue
        if action in ("light.turn_on", "light.turn_off") and all(
            t.startswith("light.") for t in targets
        ):
            continue
        if (
            action in ("switch.turn_on", "switch.turn_off")
            and set(targets) <= own_switches
        ):
            continue
        if (
            action == "alarm_control_panel.alarm_trigger"
            and armed
            and targets == [armed]
        ):
            continue
        return f"{action} is not something Sentinel may do in Home Assistant."
    return None


def refusal(config: dict[str, Any], own_switches: frozenset[str]) -> str | None:
    """Why this automation breaks the agreed limits, or None if it keeps them."""
    return _check(config.get("actions") or [], own_switches, None)
