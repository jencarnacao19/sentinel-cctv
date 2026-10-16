"""Short-lived grants for native HLS playback.

Safari's native video player cannot attach Sentinel's viewer header to the
playlist and segment requests it creates itself.  The authenticated session
endpoint therefore exchanges that header for an opaque, camera-scoped grant.
The grant contains no credential and only works while it is being used.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class HlsGrant:
    camera_id: str
    upstream_id: str
    created_at: float
    last_used: float


class HlsSessionRegistry:
    """Small in-memory registry with sliding expiry and a hard size ceiling."""

    def __init__(
        self,
        *,
        idle_seconds: float = 45.0,
        max_lifetime_seconds: float = 15 * 60.0,
        max_sessions: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle_seconds = idle_seconds
        self._max_lifetime_seconds = max_lifetime_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._grants: dict[str, HlsGrant] = {}

    def issue(self, camera_id: str, upstream_id: str) -> str:
        now = self._clock()
        self._prune(now)
        while len(self._grants) >= self._max_sessions:
            oldest = min(self._grants, key=lambda key: self._grants[key].last_used)
            self._grants.pop(oldest, None)
        token = secrets.token_urlsafe(32)
        self._grants[token] = HlsGrant(
            camera_id=camera_id,
            upstream_id=upstream_id,
            created_at=now,
            last_used=now,
        )
        return token

    def resolve(self, camera_id: str, token: str) -> HlsGrant | None:
        now = self._clock()
        self._prune(now)
        grant = self._grants.get(token)
        if grant is None or grant.camera_id != camera_id:
            return None
        grant.last_used = now
        return grant

    def clear(self) -> None:
        self._grants.clear()

    def _prune(self, now: float) -> None:
        expired = [
            token
            for token, grant in self._grants.items()
            if now - grant.last_used > self._idle_seconds
            or now - grant.created_at > self._max_lifetime_seconds
        ]
        for token in expired:
            self._grants.pop(token, None)


hls_sessions = HlsSessionRegistry()
