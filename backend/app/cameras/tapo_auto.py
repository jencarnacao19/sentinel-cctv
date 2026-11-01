"""Timed battery recordings that start without a person pressing Start.

Both triggers are opt-in per camera and both end in an ordinary bounded session
from :mod:`app.cameras.tapo_timed`, so the deadline, the shared live-view
reservation, privacy mode and the partial-footage rules all still apply.

* **The camera woke up on its own.** A standalone TC82 ignores broadcast ARP
  while it sleeps, so its address only answers once its own motion sensor (or
  the TP-Link cloud) has woken it. With the camera's MAC pinned in the host's
  neighbour table an ICMP echo still does not wake it -- a TCP connection does
  -- which makes a ping every couple of seconds a free way to notice a wake.
  Short wakes (a few seconds, the camera checking in) are ignored; one that
  lasts longer is almost always motion, and is recorded.
* **Another camera saw motion.** Opening the Direct Stream wakes the camera
  over the LAN, so motion anywhere else can be used to record this one too.

A hub-attached camera cannot use the first trigger: the hub is always awake,
so its address says nothing about the camera.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.cameras import tapo_battery, tapo_timed
from app.database.models import Camera, CameraKind
from app.database.session import session_scope
from app.events.bus import MOTION_STARTED, get_bus
from app.logging_config import get_logger
from app.system import privacy, settings_store

logger = get_logger("CAMERA")

#: One JSON object, camera id -> rules, in the settings table.
SETTING_KEY = "cameras.tapo_auto_recording"
DEFAULT_MINUTES = 2

POLL_SECONDS = 2.0
PING_TIMEOUT_SECONDS = 1.0
#: A wake shorter than this is the camera checking in, not motion.
SELF_WAKE_MIN_SECONDS = 6.0
#: No reply for this long means it went back to sleep. Longer than one missed
#: poll (interval + timeout + interval), so a single lost packet on Wi-Fi does
#: not count as a new wake.
ASLEEP_AFTER_SECONDS = 7.0
RULES_TTL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class AutoRules:
    on_wake: bool = False
    on_wake_minutes: int = DEFAULT_MINUTES
    on_motion: bool = False
    on_motion_minutes: int = DEFAULT_MINUTES

    @classmethod
    def from_stored(cls, value: Any) -> AutoRules:
        if not isinstance(value, dict):
            return cls()

        def minutes(key: str) -> int:
            try:
                number = int(value.get(key, DEFAULT_MINUTES))
            except (TypeError, ValueError):
                return DEFAULT_MINUTES
            return max(
                tapo_timed.MIN_DURATION_MINUTES, min(number, tapo_timed.MAX_DURATION_MINUTES)
            )

        return cls(
            on_wake=value.get("on_wake") is True,
            on_wake_minutes=minutes("on_wake_minutes"),
            on_motion=value.get("on_motion") is True,
            on_motion_minutes=minutes("on_motion_minutes"),
        )


def supports_wake_detection(camera: Camera) -> bool:
    return not camera.tapo_child_id


async def ping(host: str) -> bool:
    """One unprivileged ICMP echo; needs ``net.ipv4.ping_group_range``."""
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP) as sock:
        sock.setblocking(False)
        # The kernel fills in the identifier and checksum of a datagram echo.
        await loop.sock_sendto(sock, b"\x08\x00\x00\x00\x00\x00\x00\x01sentinel", (host, 0))
        try:
            await asyncio.wait_for(loop.sock_recv(sock, 64), timeout=PING_TIMEOUT_SECONDS)
        except TimeoutError:
            return False
    return True


class AutoRecorder:
    def __init__(self) -> None:
        self._rules: list[tuple[Camera, AutoRules]] | None = None
        self._loaded_at = 0.0
        self._awake_since: dict[str, float] = {}
        self._last_reply: dict[str, float] = {}
        self._handled: set[str] = set()
        self._starting: set[str] = set()
        self._ping_refused: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        subscription = get_bus().subscribe(names={MOTION_STARTED})
        self._spawn_task(self._watch(), "tapo-auto:watch")
        self._spawn_task(self._listen(subscription), "tapo-auto:motion")

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task

    def invalidate(self) -> None:
        """Pick up changed rules on the next pass instead of after the TTL."""
        self._rules = None

    # --- triggers ---------------------------------------------------------

    async def poll_once(self, now: float | None = None) -> None:
        if privacy.get_manager().state.enabled:
            return
        watched = [
            (camera, rules)
            for camera, rules in await self._load_rules()
            if rules.on_wake and supports_wake_detection(camera) and camera.host
        ]
        replies = await asyncio.gather(*(self._ping(camera) for camera, _ in watched))
        now = time.monotonic() if now is None else now
        for (camera, rules), awake in zip(watched, replies, strict=True):
            if not awake:
                continue
            if now - self._last_reply.get(camera.id, float("-inf")) > ASLEEP_AFTER_SECONDS:
                self._awake_since[camera.id] = now
                self._handled.discard(camera.id)
            self._last_reply[camera.id] = now
            if camera.id in self._handled:
                continue
            if now - self._awake_since[camera.id] < SELF_WAKE_MIN_SECONDS:
                continue
            # Once per wake, recorded or not: if a live view woke it, or a
            # recording is already running, the next chance is the next wake.
            self._handled.add(camera.id)
            self._record(camera, rules.on_wake_minutes, "woke up on its own")

    async def on_motion(self, source_camera_id: str | None, source_name: str | None) -> None:
        reason = f"motion on {source_name or 'another camera'}"
        for camera, rules in await self._load_rules():
            if rules.on_motion and camera.id != source_camera_id:
                self._record(camera, rules.on_motion_minutes, reason)

    # --- internals --------------------------------------------------------

    async def _watch(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                logger.exception("Watching Tapo battery cameras for wakes failed")
            await asyncio.sleep(POLL_SECONDS)

    async def _listen(self, subscription: Any) -> None:
        try:
            while True:
                event = await subscription.get()
                try:
                    await self.on_motion(event.data.get("camera_id"), event.data.get("camera"))
                except Exception:
                    logger.exception("Starting a Tapo battery recording on motion failed")
        finally:
            get_bus().unsubscribe(subscription)

    async def _ping(self, camera: Camera) -> bool:
        try:
            return await ping(camera.host or "")
        except PermissionError:
            if camera.id not in self._ping_refused:
                self._ping_refused.add(camera.id)
                logger.warning(
                    "This host does not allow Sentinel to ping, so a battery camera waking on its "
                    "own cannot be noticed. Allow it with net.ipv4.ping_group_range.",
                    extra={"camera": camera.name, "camera_id": camera.id},
                )
            return False
        except OSError:
            return False

    async def _load_rules(self) -> list[tuple[Camera, AutoRules]]:
        if self._rules is None or time.monotonic() - self._loaded_at > RULES_TTL_SECONDS:
            async with session_scope() as db:
                stored = await settings_store.get_setting(db, SETTING_KEY, {})
                cameras = (
                    await db.scalars(
                        select(Camera).where(
                            Camera.kind == CameraKind.TAPO_BATTERY, Camera.enabled.is_(True)
                        )
                    )
                ).all()
            stored = stored if isinstance(stored, dict) else {}
            rules = [(camera, AutoRules.from_stored(stored.get(camera.id))) for camera in cameras]
            self._rules = [(c, r) for c, r in rules if r.on_wake or r.on_motion]
            self._loaded_at = time.monotonic()
        return self._rules

    def _record(self, camera: Camera, minutes: int, reason: str) -> None:
        timed = tapo_timed.get_manager()
        if camera.id in self._starting or timed.active(camera.id):
            return
        self._starting.add(camera.id)
        self._spawn_task(self._begin(camera, minutes, reason), f"tapo-auto:{camera.id}")

    async def _begin(self, camera: Camera, minutes: int, reason: str) -> None:
        try:
            await tapo_timed.get_manager().begin(
                camera,
                duration_seconds=minutes * 60,
                battery_percent=None,
                requested_by=f"automatic: {reason}",
            )
        except (tapo_timed.TimedRecordingError, tapo_battery.TapoBatteryError) as exc:
            # Busy (someone is watching live), privacy mode, or the camera did
            # not answer. Nothing to retry: the next trigger is the next chance.
            logger.info(
                "Automatic Tapo battery recording not started",
                extra={"camera": camera.name, "camera_id": camera.id, "detail": str(exc)},
            )
        finally:
            self._starting.discard(camera.id)

    def _spawn_task(self, coroutine: Any, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


_recorder: AutoRecorder | None = None


def get_recorder() -> AutoRecorder:
    global _recorder
    if _recorder is None:
        _recorder = AutoRecorder()
    return _recorder


def set_recorder(recorder: AutoRecorder | None) -> None:
    """Test helper."""
    global _recorder
    _recorder = recorder
