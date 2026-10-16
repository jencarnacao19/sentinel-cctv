"""Health reporting.

Subsystems register a probe here as they come up, so the health endpoint always
reflects what is actually running rather than a hard-coded list. A component
that is switched off (no cameras configured yet, go2rtc unmanaged) reports
``skipped`` and does not drag the overall status down -- an idle-but-correct
install is healthy, not degraded.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.logging_config import get_logger

logger = get_logger("SYSTEM")

PROBE_TIMEOUT_SECONDS = 5.0


class ComponentState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    state: ComponentState
    detail: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.state in (ComponentState.OK, ComponentState.SKIPPED)


ProbeResult = ComponentHealth | bool
Probe = Callable[[], ProbeResult | Awaitable[ProbeResult]]

_probes: dict[str, Probe] = {}
#: Components that must be healthy for the overall status to be "healthy".
_critical: set[str] = set()


def register(name: str, probe: Probe, *, critical: bool = True) -> None:
    _probes[name] = probe
    if critical:
        _critical.add(name)
    else:
        _critical.discard(name)


def unregister(name: str) -> None:
    _probes.pop(name, None)
    _critical.discard(name)


def reset() -> None:
    """Test helper."""
    _probes.clear()
    _critical.clear()


def _normalise(result: ProbeResult) -> ComponentHealth:
    if isinstance(result, ComponentHealth):
        return result
    return ComponentHealth(state=ComponentState.OK if result else ComponentState.FAILED)


async def _run_probe(name: str, probe: Probe) -> ComponentHealth:
    try:
        outcome = probe()
        if inspect.isawaitable(outcome):
            outcome = await asyncio.wait_for(outcome, timeout=PROBE_TIMEOUT_SECONDS)
        return _normalise(outcome)
    except TimeoutError:
        logger.warning("Health probe timed out", extra={"component": name})
        return ComponentHealth(state=ComponentState.FAILED, detail="Probe timed out.")
    except Exception as exc:
        logger.warning("Health probe failed", extra={"component": name}, exc_info=True)
        return ComponentHealth(state=ComponentState.FAILED, detail=str(exc))


async def collect() -> tuple[str, dict[str, ComponentHealth]]:
    """Run every probe concurrently and fold the results into one status."""
    names = list(_probes)
    results = await asyncio.gather(*(_run_probe(name, _probes[name]) for name in names))
    components = dict(zip(names, results, strict=True))

    status = "healthy"
    for name, health in components.items():
        if health.state is ComponentState.FAILED:
            status = "unhealthy" if name in _critical else "degraded"
            if name in _critical:
                break
        elif health.state is ComponentState.DEGRADED and status == "healthy":
            status = "degraded"
    return status, components
