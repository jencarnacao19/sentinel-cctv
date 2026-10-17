"""Named motion zones: where to look, where to ignore, and how sensitive.

A camera can have two kinds of zone:

* **Ignore zones** are cut out of the picture before anything is measured: a
  road, a tree, a neighbour's window.
* **Detection zones** are the only places motion counts once any exist. Each
  one has a name, its own on/off state in Home Assistant and, optionally, its
  own sensitivity.

Everything stays on the CPU and on one background model per camera: a zone's
sensitivity changes how large a movement inside it has to be, not how many
models run. See :mod:`app.motion.detector`.

Zones live in the camera's ``motion_zones`` column as JSON. The first version
stored bare polygons; those still load, as ignore zones.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum

from app.logging_config import get_logger

logger = get_logger("MOTION")

FORMAT_VERSION = 2

#: Ceilings that keep one camera's analysis cost bounded. Each detection zone
#: adds one contour pass over a 640x360 frame, roughly a tenth of a millisecond.
MAX_ZONES = 32
MAX_DETECT_ZONES = 16
MIN_POINTS = 3
MAX_POINTS = 128
MAX_NAME_LENGTH = 48

ZONE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

Point = tuple[float, float]


class ZoneKind(StrEnum):
    DETECT = "detect"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class MotionZone:
    id: str
    name: str
    kind: ZoneKind
    points: tuple[Point, ...]
    enabled: bool = True
    sensitivity: int | None = None
    """0-100 for a detection zone, or ``None`` to follow the camera's."""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind.value,
            "enabled": self.enabled,
            "sensitivity": self.sensitivity,
            "points": [[round(x, 4), round(y, 4)] for x, y in self.points],
        }


def new_zone_id() -> str:
    return f"z{secrets.token_hex(4)}"


def parse_zones(raw: str | None) -> list[MotionZone]:
    """Decode a camera's zones, tolerating anything malformed.

    A bad value must not stop motion detection for that camera; the worst
    outcome of skipping a zone is more events, not none.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Motion zones could not be decoded; ignoring them")
        return []

    if isinstance(data, list):
        # Version 1: a list of bare ignore polygons.
        return [
            MotionZone(
                id=f"ignore-{index}",
                name=f"Ignored area {index}",
                kind=ZoneKind.IGNORE,
                points=points,
            )
            for index, points in enumerate(
                (p for p in (_points(entry) for entry in data) if p is not None), start=1
            )
        ]

    if not isinstance(data, dict) or not isinstance(data.get("zones"), list):
        return []
    zones: list[MotionZone] = []
    for entry in data["zones"]:
        zone = _zone(entry)
        if zone is not None:
            zones.append(zone)
    return zones


def serialise_zones(zones: list[MotionZone]) -> str | None:
    if not zones:
        return None
    return json.dumps({"version": FORMAT_VERSION, "zones": [zone.to_dict() for zone in zones]})


def detection_zones(zones: list[MotionZone]) -> list[MotionZone]:
    """The enabled detection zones, which are what Home Assistant gets sensors for."""
    return [zone for zone in zones if zone.enabled and zone.kind is ZoneKind.DETECT]


def _points(value: object) -> tuple[Point, ...] | None:
    if not isinstance(value, list) or not MIN_POINTS <= len(value) <= MAX_POINTS:
        return None
    points: list[Point] = []
    for point in value:
        if (
            isinstance(point, (list, tuple))
            and len(point) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in point)
        ):
            x, y = float(point[0]), float(point[1])
            if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                points.append((x, y))
    return tuple(points) if len(points) >= MIN_POINTS else None


def _zone(entry: object) -> MotionZone | None:
    if not isinstance(entry, dict):
        return None
    zone_id = entry.get("id")
    name = entry.get("name")
    points = _points(entry.get("points"))
    raw_kind = entry.get("kind")
    if not isinstance(raw_kind, str):
        return None
    try:
        kind = ZoneKind(raw_kind)
    except ValueError:
        return None
    if (
        not isinstance(zone_id, str)
        or not ZONE_ID_PATTERN.fullmatch(zone_id)
        or not isinstance(name, str)
        or not name.strip()
        or points is None
    ):
        return None
    sensitivity = entry.get("sensitivity")
    if not (
        sensitivity is None or (isinstance(sensitivity, int) and not isinstance(sensitivity, bool))
    ):
        sensitivity = None
    if isinstance(sensitivity, int):
        sensitivity = max(0, min(100, sensitivity))
    return MotionZone(
        id=zone_id,
        name=name.strip()[:MAX_NAME_LENGTH],
        kind=kind,
        points=points,
        enabled=entry.get("enabled") is not False,
        sensitivity=sensitivity if kind is ZoneKind.DETECT else None,
    )
