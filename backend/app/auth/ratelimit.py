"""In-process rate limiting for authentication and other expensive endpoints.

Sentinel is a single-process LAN service, so an in-memory limiter is both
sufficient and one less moving part than Redis. It protects the two things that
matter: password guessing against /auth/login, and CPU-heavy endpoints such as
discovery scans and RTSP probes.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class RateLimitResult:
    allowed: bool
    retry_after: float = 0.0
    remaining: int = 0


@dataclass(slots=True)
class SlidingWindowLimiter:
    """Allow ``limit`` events per ``window_seconds`` per key."""

    limit: int
    window_seconds: float
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check(self, key: str, *, now: float | None = None) -> RateLimitResult:
        current = now if now is not None else time.monotonic()
        async with self._lock:
            hits = self._hits.setdefault(key, deque())
            cutoff = current - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self.limit:
                retry_after = max(0.0, hits[0] + self.window_seconds - current)
                return RateLimitResult(allowed=False, retry_after=retry_after, remaining=0)

            hits.append(current)
            self._prune(current)
            return RateLimitResult(allowed=True, remaining=self.limit - len(hits))

    async def reset(self, key: str | None = None) -> None:
        async with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def _prune(self, current: float) -> None:
        """Drop keys whose window has fully elapsed, so a long uptime with many
        distinct client IPs cannot grow this dict without bound."""
        if len(self._hits) < 512:
            return
        cutoff = current - self.window_seconds
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]


#: Five failed logins per minute per client address. Generous for a human,
#: useless for a dictionary attack.
login_limiter = SlidingWindowLimiter(limit=5, window_seconds=60.0)

#: Token creation is an admin action; a low ceiling is fine.
token_limiter = SlidingWindowLimiter(limit=10, window_seconds=300.0)

#: Network discovery and RTSP probes spawn subprocesses and touch the LAN.
expensive_limiter = SlidingWindowLimiter(limit=20, window_seconds=60.0)

#: Hold-to-move refreshes twice per second. This ceiling leaves room for
#: several simultaneous cameras while preventing a leaked viewer grant from
#: flooding a camera's tiny ONVIF HTTP server.
ptz_limiter = SlidingWindowLimiter(limit=60, window_seconds=10.0)

#: Manual camera recovery restarts that camera's recorder and motion worker.
#: Sentinel's supervisors already retry on their own, so this exists only to
#: skip the backoff ladder -- a low ceiling stops a stuck browser tab or a
#: retry loop from cycling a camera continuously.
recovery_limiter = SlidingWindowLimiter(limit=6, window_seconds=300.0)


async def reset_all_limiters() -> None:
    """Test helper."""
    for limiter in (
        login_limiter,
        token_limiter,
        expensive_limiter,
        ptz_limiter,
        recovery_limiter,
    ):
        await limiter.reset()
