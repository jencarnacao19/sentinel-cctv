"""Persistent, fail-safe global camera privacy mode.

Privacy mode is deliberately enforced below the browser: it removes every
go2rtc stream, stops recording and motion workers, and refuses direct camera
operations. Historical footage remains available because enabling privacy
must not erase evidence that was already recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.base import utcnow
from app.logging_config import get_logger
from app.system import settings_store

logger = get_logger("SYSTEM")


class PrivacyReason(StrEnum):
    PRIVACY = "privacy"
    GUESTS = "guests"
    PRIVATE_EVENT = "private_event"
    MAINTENANCE = "maintenance"
    TEMPORARY = "temporary"


REASON_LABELS: dict[PrivacyReason, str] = {
    PrivacyReason.PRIVACY: "Privacy requested",
    PrivacyReason.GUESTS: "Guests are present",
    PrivacyReason.PRIVATE_EVENT: "Private event in progress",
    PrivacyReason.MAINTENANCE: "Camera maintenance in progress",
    PrivacyReason.TEMPORARY: "Temporarily disabled by an administrator",
}


@dataclass(frozen=True, slots=True)
class PrivacyState:
    enabled: bool = False
    reason: PrivacyReason | None = None
    changed_at: datetime | None = None
    changed_by: str | None = None

    @property
    def reason_label(self) -> str | None:
        return REASON_LABELS.get(self.reason) if self.reason is not None else None

    @property
    def message(self) -> str | None:
        label = self.reason_label
        return f"Cameras are paused for privacy: {label}." if self.enabled and label else None


class PrivacyStatus(BaseModel):
    enabled: bool
    reason: PrivacyReason | None
    reason_label: str | None
    message: str | None
    changed_at: datetime | None
    changed_by: str | None
    reasons: dict[PrivacyReason, str]


class PrivacyUpdate(BaseModel):
    enabled: bool
    reason: PrivacyReason | None = None

    @model_validator(mode="after")
    def require_reason_when_enabling(self) -> PrivacyUpdate:
        if self.enabled and self.reason is None:
            raise ValueError("Choose a reason before enabling privacy mode.")
        if not self.enabled:
            self.reason = None
        return self


class PrivacyTransitionError(RuntimeError):
    """One or more runtime components did not apply the requested state."""


class PrivacyManager:
    def __init__(self) -> None:
        self._state = PrivacyState()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> PrivacyState:
        return self._state

    def status(self) -> PrivacyStatus:
        state = self._state
        return PrivacyStatus(
            enabled=state.enabled,
            reason=state.reason,
            reason_label=state.reason_label,
            message=state.message,
            changed_at=state.changed_at,
            changed_by=state.changed_by,
            reasons=REASON_LABELS,
        )

    async def load(self, session: AsyncSession) -> PrivacyState:
        """Load the last safe state before any camera subsystem starts."""
        raw = await settings_store.get_setting(
            session, settings_store.PRIVACY_MODE, {"enabled": False}
        )
        self._state = self._decode(raw)
        return self._state

    async def update(
        self,
        session: AsyncSession,
        *,
        enabled: bool,
        reason: PrivacyReason | None,
        changed_by: str,
    ) -> PrivacyStatus:
        async with self._lock:
            if enabled:
                if reason is None:
                    raise ValueError("A privacy reason is required.")
                state = PrivacyState(
                    enabled=True,
                    reason=reason,
                    changed_at=utcnow(),
                    changed_by=changed_by,
                )
                # Block new routes and persist first. If a process stop then
                # fails, a restart still comes back in the fail-safe state.
                self._state = state
                await self._persist(session, state)
                await session.commit()
                errors = await self._apply_runtime(True)
                if errors:
                    # Process termination can race with a camera reconnect.
                    # Retry once while the persistent fail-safe state is already active.
                    errors = await self._apply_runtime(True)
                if errors:
                    raise PrivacyTransitionError(
                        "Privacy mode is active, but a camera service did not stop cleanly."
                    )
            else:
                if not self._state.enabled:
                    return self.status()
                errors = await self._apply_runtime(False)
                if errors:
                    # Do not advertise cameras as available after a partial
                    # resume. Put every component back into the safe state.
                    await self._apply_runtime(True)
                    raise PrivacyTransitionError(
                        "Cameras could not all resume. Privacy mode remains active."
                    )
                state = PrivacyState(
                    enabled=False,
                    changed_at=utcnow(),
                    changed_by=changed_by,
                )
                self._state = state
                await self._persist(session, state)
                await session.commit()

            return self.status()

    async def apply_loaded_state(self) -> None:
        """Prime managers before their normal lifespan start methods run."""
        enabled = self._state.enabled
        from app.motion.manager import get_manager as get_motion_manager
        from app.recording.manager import get_manager as get_recording_manager
        from app.streaming.manager import get_manager as get_streaming_manager

        get_streaming_manager().initialise_privacy(enabled)
        get_recording_manager().initialise_privacy(enabled)
        get_motion_manager().initialise_privacy(enabled)

    async def _apply_runtime(self, enabled: bool) -> list[Exception]:
        from app.motion.manager import get_manager as get_motion_manager
        from app.recording.manager import get_manager as get_recording_manager
        from app.streaming.manager import get_manager as get_streaming_manager
        from app.streaming.router import close_active_streams

        if enabled:
            await close_active_streams(self._state.message or "Cameras paused for privacy.")
            results = list(
                await asyncio.gather(
                    self._capture(get_streaming_manager().set_privacy_paused(True)),
                    self._capture(get_recording_manager().set_privacy_paused(True)),
                    self._capture(get_motion_manager().set_privacy_paused(True)),
                )
            )
        else:
            streaming = get_streaming_manager()
            try:
                await streaming.set_privacy_paused(False)
                ready = await streaming.wait_until_ready()
                if not get_settings().testing and not ready:
                    raise RuntimeError("The local live-stream relay did not become ready.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - returned for fail-safe rollback
                results = [exc]
            else:
                results = list(
                    await asyncio.gather(
                        self._capture(get_recording_manager().set_privacy_paused(False)),
                        self._capture(get_motion_manager().set_privacy_paused(False)),
                    )
                )

        errors = [result for result in results if isinstance(result, Exception)]
        for error in errors:
            logger.error(
                "Privacy transition component failed",
                extra={"error": str(error), "error_type": type(error).__name__},
            )
        return errors

    @staticmethod
    async def _capture(operation: Awaitable[None]) -> Exception | None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - caller performs fail-safe rollback
            return exc
        return None

    async def _persist(self, session: AsyncSession, state: PrivacyState) -> None:
        await settings_store.set_setting(
            session,
            settings_store.PRIVACY_MODE,
            {
                "enabled": state.enabled,
                "reason": state.reason.value if state.reason else None,
                "changed_at": state.changed_at.isoformat() if state.changed_at else None,
                "changed_by": state.changed_by,
            },
        )

    @staticmethod
    def _decode(raw: Any) -> PrivacyState:
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            return PrivacyState()
        try:
            reason = PrivacyReason(str(raw.get("reason")))
        except ValueError:
            reason = PrivacyReason.PRIVACY
        changed_at: datetime | None = None
        if isinstance(raw.get("changed_at"), str):
            with contextlib.suppress(ValueError):
                changed_at = datetime.fromisoformat(raw["changed_at"])
        changed_by = raw.get("changed_by") if isinstance(raw.get("changed_by"), str) else None
        return PrivacyState(True, reason, changed_at, changed_by)


_manager: PrivacyManager | None = None


def get_manager() -> PrivacyManager:
    global _manager
    if _manager is None:
        _manager = PrivacyManager()
    return _manager


def set_manager(manager: PrivacyManager | None) -> None:
    """Test helper."""
    global _manager
    _manager = manager


def require_camera_access() -> None:
    """Refuse any operation that would contact a camera while privacy is active."""
    state = get_manager().state
    if state.enabled:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=state.message or "Cameras are paused for privacy.",
        )
