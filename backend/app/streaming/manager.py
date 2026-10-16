"""Ownership of the go2rtc process and its stream list.

Two paths deliberately kept separate:

* **Cold start** reads the generated ``go2rtc.yaml``. That file is the source of
  truth after a reboot, so a machine that comes back up with no help from the
  API still has every camera configured.
* **Hot changes** go through go2rtc's REST API. Adding a camera must not
  restart go2rtc, because a restart would drop every live view and, once the
  recorders pull from go2rtc, interrupt every recording in the house.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection, Sequence

from app.cameras.service import list_cameras
from app.config import Settings, get_settings
from app.database.models import Camera
from app.database.session import session_scope
from app.logging_config import get_logger
from app.streaming.client import Go2RtcClient, Go2RtcUnavailableError
from app.streaming.config import build_streams, write_config
from app.supervisor import ProcessState, SupervisedProcess
from app.system import health

logger = get_logger("GO2RTC")

#: How long to wait for go2rtc's API to answer after the process starts.
STARTUP_TIMEOUT_SECONDS = 20.0


class Go2RtcManager:
    """Starts go2rtc, keeps its stream list in step with the camera list."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = Go2RtcClient(self._settings)
        self._process: SupervisedProcess | None = None
        self._readiness_task: asyncio.Task[bool] | None = None
        self._sync_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._privacy_paused = False

    # --- Lifecycle -------------------------------------------------------

    @property
    def managed(self) -> bool:
        """False when the operator runs their own go2rtc elsewhere."""
        return self._settings.manage_go2rtc

    @property
    def privacy_paused(self) -> bool:
        return self._privacy_paused

    def initialise_privacy(self, paused: bool) -> None:
        """Set the persisted state before the normal lifespan start."""
        self._privacy_paused = paused

    async def start(self) -> None:
        """Write the config and start go2rtc.

        Deliberately non-blocking. Waiting here for go2rtc's API would delay the
        whole application start-up behind a subsystem that only affects live
        view; the readiness poll runs in the background and health reports the
        outcome.
        """
        async with session_scope() as session:
            cameras = await list_cameras(session)
        if self._privacy_paused:
            cameras = []
        write_config(self._settings, cameras)

        if self._settings.testing:
            logger.debug("Testing mode: go2rtc process not started")
            return

        if self._privacy_paused:
            logger.info("Privacy mode active: go2rtc remains stopped")
            return

        if self.managed:
            self._process = SupervisedProcess(
                "go2rtc",
                command_factory=self._build_command,
                category="GO2RTC",
            )
            await self._process.start()
        else:
            logger.info("go2rtc is not managed by Sentinel; using the configured endpoint")

        self._readiness_task = asyncio.create_task(
            self._wait_until_ready(timeout_seconds=STARTUP_TIMEOUT_SECONDS),
            name="go2rtc-readiness",
        )

    async def stop(self) -> None:
        if self._readiness_task is not None:
            self._readiness_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._readiness_task
            self._readiness_task = None
        if self._process is not None:
            await self._process.stop()
            self._process = None
        await self._client.aclose()
        self._ready.clear()

    async def set_privacy_paused(self, paused: bool) -> None:
        """Remove all sources and stop the relay, or restore it locally."""
        if paused == self._privacy_paused:
            if paused and self.managed and self._process is None:
                return
            if paused and not self.managed:
                await self.sync([])
                return
            if not paused and (not self.managed or self._process is not None):
                return

        self._privacy_paused = paused
        if paused:
            # An empty cold-start config prevents a supervisor/container restart
            # from reconnecting to a camera while privacy remains enabled.
            write_config(self._settings, [])
            if self.managed:
                await self.stop()
            else:
                await self.sync([])
            return

        if self.managed:
            await self.start()
        else:
            await self.sync()

    def _build_command(self) -> Sequence[str]:
        return [
            self._settings.go2rtc_binary,
            "-config",
            str(self._settings.go2rtc_config_path),
        ]

    async def _wait_until_ready(self, *, timeout_seconds: float) -> bool:
        """Poll the API until it answers or the timeout expires."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            if await self._client.is_alive():
                self._ready.set()
                logger.info("go2rtc is ready", extra={"url": self._settings.go2rtc_api_url})
                return True
            await asyncio.sleep(0.5)

        # Not fatal. Sentinel stays up and reports go2rtc as degraded rather than
        # refusing to boot, so the user can reach the interface and see why.
        logger.warning(
            "go2rtc did not become ready in time",
            extra={"timeout_seconds": timeout_seconds, "url": self._settings.go2rtc_api_url},
        )
        return False

    @property
    def is_ready(self) -> bool:
        if self._privacy_paused or not self._ready.is_set():
            return False
        if self._settings.testing:
            return True
        # The readiness event records the last successful probe. A managed
        # process can subsequently exit, so process liveness must also hold or
        # reliability would report a dead relay as ready until another health
        # request happened to clear the event.
        return not self.managed or bool(self._process and self._process.is_running)

    async def wait_until_ready(self) -> bool:
        """Wait for the start-up readiness probe already running in background.

        The web application can start serving diagnostics without go2rtc, but
        recorders that use its RTSP relay must not dial it before the listener is
        ready. Awaiting the existing task avoids a second polling loop and keeps
        the same bounded twenty-second failure behaviour.
        """
        if self._ready.is_set():
            return True
        if self._readiness_task is None:
            return False
        return await self._readiness_task

    # --- Stream synchronisation ------------------------------------------

    async def sync(
        self,
        cameras: Sequence[Camera] | None = None,
        *,
        replace_streams: Collection[str] = (),
    ) -> None:
        """Reconcile go2rtc's stream list with the camera list.

        ``replace_streams`` identifies existing names whose source URL or
        credentials changed. go2rtc keeps an added stream in memory, so merely
        rewriting its cold-start config is not enough for an edited camera.
        """
        async with self._sync_lock:
            if cameras is None:
                async with session_scope() as session:
                    cameras = await list_cameras(session)

            if self._privacy_paused:
                cameras = []

            write_config(self._settings, cameras)
            desired = build_streams(
                cameras,
                go2rtc_host=self._settings.go2rtc_host,
                go2rtc_rtsp_port=self._settings.go2rtc_rtsp_port,
            )

            if self._settings.testing:
                # The generated config is still written and asserted on; only the
                # loopback call to a go2rtc that is not running is skipped.
                return

            try:
                current = await self._client.list_streams()
            except Go2RtcUnavailableError:
                # go2rtc is down. The config file on disk is already correct, so
                # the supervisor's next restart picks the change up.
                logger.info("go2rtc unavailable; configuration written for its next start")
                self._ready.clear()
                return

            self._ready.set()

            replacements = set(replace_streams)
            for name, sources in desired.items():
                if name not in current or name in replacements:
                    await self._safe_add(name, sources, replacing=name in current)

            for name in current:
                if name.startswith("cam_") and name not in desired:
                    await self._safe_delete(name)

    async def _safe_add(
        self, name: str, sources: Sequence[str], *, replacing: bool = False
    ) -> None:
        try:
            await self._client.add_stream(name, sources)
            logger.info(
                "Stream replaced in go2rtc" if replacing else "Stream added to go2rtc",
                extra={"stream": name},
            )
        except Go2RtcUnavailableError:
            logger.warning("Could not add stream to go2rtc", extra={"stream": name})

    async def _safe_delete(self, name: str) -> None:
        try:
            await self._client.delete_stream(name)
            logger.info("Stream removed from go2rtc", extra={"stream": name})
        except Go2RtcUnavailableError:
            logger.warning("Could not remove stream from go2rtc", extra={"stream": name})

    # --- Reporting -------------------------------------------------------

    @property
    def client(self) -> Go2RtcClient:
        return self._client

    def process_status(self) -> dict[str, object] | None:
        return self._process.status() if self._process else None

    async def health_probe(self) -> health.ComponentHealth:
        """Health contribution for ``/system/health``.

        An install with no cameras reports ``skipped``: go2rtc having nothing to
        do is not a degraded system.
        """
        if self._privacy_paused:
            return health.ComponentHealth(
                state=health.ComponentState.SKIPPED,
                detail="Paused by privacy mode.",
                data={"streams": 0},
            )

        async with session_scope() as session:
            camera_count = len(await list_cameras(session, enabled_only=True))

        if camera_count == 0:
            return health.ComponentHealth(
                state=health.ComponentState.SKIPPED,
                detail="No enabled cameras yet.",
            )

        alive = await self._client.is_alive()
        if alive:
            self._ready.set()
            return health.ComponentHealth(
                state=health.ComponentState.OK,
                data={"streams": camera_count},
            )

        self._ready.clear()
        status = self._process.status() if self._process else None
        detail = "go2rtc is not responding."
        if status and status.get("state") == ProcessState.RESTARTING.value:
            detail = "go2rtc is restarting."
        return health.ComponentHealth(state=health.ComponentState.FAILED, detail=detail)


_manager: Go2RtcManager | None = None


def get_manager() -> Go2RtcManager:
    global _manager
    if _manager is None:
        _manager = Go2RtcManager()
    return _manager


def set_manager(manager: Go2RtcManager | None) -> None:
    """Test helper."""
    global _manager
    _manager = manager
