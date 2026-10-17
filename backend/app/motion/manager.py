"""Ownership of the motion workers."""

from __future__ import annotations

import asyncio

from app.cameras.service import list_cameras
from app.config import Settings, get_settings
from app.database.models import Camera, CameraKind, RecordingMode
from app.database.session import session_scope
from app.logging_config import get_logger
from app.motion import service
from app.motion.worker import MotionWorker
from app.system import health

logger = get_logger("MOTION")


class MotionManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._workers: dict[str, MotionWorker] = {}
        self._lock = asyncio.Lock()
        self._privacy_paused = False

    async def start(self) -> None:
        async with session_scope() as session:
            # A crash leaves events with no end time, which the timeline would
            # draw as still running. Close them before anything new begins.
            await service.close_orphaned_events(session)

        if self._settings.testing:
            return
        await self.sync()

    async def stop(self) -> None:
        async with self._lock:
            await asyncio.gather(
                *(worker.stop() for worker in self._workers.values()), return_exceptions=True
            )
            self._workers.clear()

    @property
    def privacy_paused(self) -> bool:
        return self._privacy_paused

    def initialise_privacy(self, paused: bool) -> None:
        self._privacy_paused = paused

    async def set_privacy_paused(self, paused: bool) -> None:
        if paused == self._privacy_paused and (not paused or not self._workers):
            return
        self._privacy_paused = paused
        if paused:
            async with self._lock:
                workers = list(self._workers.items())
                results = await asyncio.gather(
                    *(worker.stop() for _, worker in workers),
                    return_exceptions=True,
                )
                self._workers = {
                    camera_id: worker
                    for (camera_id, worker), result in zip(workers, results, strict=True)
                    if isinstance(result, BaseException) and worker.is_running
                }
                failures = [result for result in results if isinstance(result, BaseException)]
                if failures:
                    raise RuntimeError(
                        f"{len(failures)} motion worker(s) did not stop for privacy mode."
                    )
            logger.info("All motion detection paused by privacy mode")
        else:
            logger.info("Privacy mode ended; resuming configured motion detection")
            await self.sync()

    async def sync(self) -> None:
        """Match the running workers to the cameras that want motion detection."""
        if self._settings.testing:
            return

        async with session_scope() as session:
            cameras = await list_cameras(session)
            config = await service.load_aggregator_config(session)

        wanted = {camera.id: camera for camera in cameras if self._should_detect(camera)}

        async with self._lock:
            for camera_id in list(self._workers):
                if camera_id not in wanted:
                    await self._stop_worker(camera_id)

            for camera_id, camera in wanted.items():
                if camera_id not in self._workers:
                    worker = MotionWorker(camera, self._settings, config)
                    self._workers[camera_id] = worker
                    await worker.start()
                    logger.info(
                        "Motion worker started",
                        extra={"camera": camera.name, "camera_id": camera_id},
                    )

    def _should_detect(self, camera: Camera) -> bool:
        if self._privacy_paused:
            return False
        if camera.kind is not CameraKind.RTSP:
            return False
        if not camera.enabled or not camera.motion_enabled:
            return False
        # Detecting motion on a camera that records nothing produces events with
        # no footage behind them.
        return camera.recording_mode is not RecordingMode.OFF

    async def restart_camera(self, camera_id: str) -> None:
        """Rebuild one camera's worker after its settings changed.

        The worker holds a compiled zone mask and detector thresholds, so a
        changed sensitivity or ignore zone only takes effect on a fresh one.
        """
        async with self._lock:
            await self._stop_worker(camera_id)
        await self.sync()

    async def _stop_worker(self, camera_id: str) -> None:
        worker = self._workers.pop(camera_id, None)
        if worker is None:
            return
        await worker.stop()
        logger.info("Motion worker stopped", extra={"camera_id": camera_id})

    def worker_status(self) -> list[dict[str, object]]:
        return [worker.status() for worker in self._workers.values()]

    def active_zones(self) -> dict[str, set[str]]:
        """Detection zones currently in motion, by camera id."""
        return {worker.camera_id: worker.active_zone_ids for worker in self._workers.values()}

    async def health_probe(self) -> health.ComponentHealth:
        if self._privacy_paused:
            return health.ComponentHealth(
                state=health.ComponentState.SKIPPED,
                detail="Paused by privacy mode.",
                data={"expected": 0, "running": 0},
            )

        async with session_scope() as session:
            cameras = await list_cameras(session)
        expected = [camera for camera in cameras if self._should_detect(camera)]

        if not expected:
            return health.ComponentHealth(
                state=health.ComponentState.SKIPPED,
                detail="No cameras have motion detection enabled.",
            )

        running = sum(1 for worker in self._workers.values() if worker.is_running)
        data: dict[str, object] = {"expected": len(expected), "running": running}
        if running == 0:
            return health.ComponentHealth(
                state=health.ComponentState.FAILED,
                detail="No motion worker is running.",
                data=data,
            )
        if running < len(expected):
            return health.ComponentHealth(
                state=health.ComponentState.DEGRADED,
                detail=f"{len(expected) - running} of {len(expected)} motion workers are down.",
                data=data,
            )
        return health.ComponentHealth(state=health.ComponentState.OK, data=data)


_manager: MotionManager | None = None


def get_manager() -> MotionManager:
    global _manager
    if _manager is None:
        _manager = MotionManager()
    return _manager


def set_manager(manager: MotionManager | None) -> None:
    """Test helper."""
    global _manager
    _manager = manager
