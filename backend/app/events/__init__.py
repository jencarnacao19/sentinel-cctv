"""In-process event bus shared by every subsystem."""

from __future__ import annotations

from app.events.bus import (
    CAMERA_ADDED,
    CAMERA_OFFLINE,
    CAMERA_ONLINE,
    CAMERA_REMOVED,
    EVENT_NAMES,
    MOTION_ENDED,
    MOTION_STARTED,
    MOTION_ZONE_ENDED,
    MOTION_ZONE_STARTED,
    RECORDING_STARTED,
    RECORDING_STOPPED,
    STORAGE_CRITICAL,
    STORAGE_WARNING,
    Event,
    EventBus,
    EventName,
    Subscription,
    get_bus,
    publish,
    reset_bus,
)

__all__ = [
    "CAMERA_ADDED",
    "CAMERA_OFFLINE",
    "CAMERA_ONLINE",
    "CAMERA_REMOVED",
    "EVENT_NAMES",
    "MOTION_ENDED",
    "MOTION_STARTED",
    "MOTION_ZONE_ENDED",
    "MOTION_ZONE_STARTED",
    "RECORDING_STARTED",
    "RECORDING_STOPPED",
    "STORAGE_CRITICAL",
    "STORAGE_WARNING",
    "Event",
    "EventBus",
    "EventName",
    "Subscription",
    "get_bus",
    "publish",
    "reset_bus",
]
