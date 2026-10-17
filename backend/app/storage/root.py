"""Resolution and validation of the recordings root.

The single most dangerous failure mode in a recorder is a storage mount that
disappears: the directory still exists, writes still succeed, and the system disk
quietly fills with footage until the whole machine stops. Sentinel refuses to
write in that situation rather than discovering it at 100% full.

The marker file is what makes the difference detectable. It is created once when
the storage is first accepted; if it later vanishes while the directory still
exists, the mount is gone underneath us.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.system import settings_store

logger = get_logger("STORAGE")

#: Written into the storage root the first time it is used.
MARKER_FILENAME = ".sentinel-storage"

MARKER_CONTENT = (
    "This directory holds Sentinel CCTV recordings.\n"
    "Sentinel checks for this file to detect a storage mount that has gone away.\n"
    "Do not delete it while Sentinel is running.\n"
)


class StorageUnavailableError(RuntimeError):
    """The recordings root cannot be used. Recording must not start."""


@dataclass(frozen=True, slots=True)
class StorageStatus:
    path: Path
    available: bool
    reason: str | None = None
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0

    @property
    def used_fraction(self) -> float:
        return self.used_bytes / self.total_bytes if self.total_bytes else 0.0


async def get_storage_root(session: AsyncSession, settings: Settings | None = None) -> Path:
    """The configured recordings root, falling back to the packaged default."""
    settings = settings or get_settings()
    configured = await settings_store.get_setting(session, settings_store.STORAGE_PATH, None)
    return Path(configured) if configured else settings.recordings_dir


def shares_system_disk(root: Path, data_dir: Path) -> bool:
    """Whether recordings sit on the same filesystem as Sentinel's own data.

    In the standard install that filesystem is the container's 16 GB system
    disk, which footage fills within days. A dedicated disk mounted for
    recordings is a different filesystem.
    """
    try:
        return os.stat(root).st_dev == os.stat(data_dir).st_dev
    except OSError:
        return False


def write_marker(root: Path) -> None:
    marker = root / MARKER_FILENAME
    if not marker.exists():
        marker.write_text(MARKER_CONTENT, encoding="utf-8")


def check_storage(root: Path, *, require_marker: bool = True) -> StorageStatus:
    """Decide whether ``root`` is safe to record into."""
    if not root.exists():
        return StorageStatus(
            path=root, available=False, reason=f"Storage path does not exist: {root}"
        )
    if not root.is_dir():
        return StorageStatus(
            path=root, available=False, reason=f"Storage path is not a directory: {root}"
        )
    if not os.access(root, os.W_OK):
        return StorageStatus(
            path=root, available=False, reason=f"Storage path is not writable: {root}"
        )

    if require_marker and not (root / MARKER_FILENAME).exists():
        # The directory exists and is writable but the marker is gone. On a
        # mountpoint that means the filesystem was unmounted and we are now
        # looking at the empty directory underneath -- writing here would fill
        # the system disk.
        return StorageStatus(
            path=root,
            available=False,
            reason=(
                f"{root} no longer looks like the configured storage. "
                "If it is a mountpoint, the filesystem is not mounted. "
                "Recording is suspended to protect the system disk."
            ),
        )

    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        return StorageStatus(path=root, available=False, reason=f"Cannot read disk usage: {exc}")

    return StorageStatus(
        path=root,
        available=True,
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
    )


def prepare_storage(root: Path) -> StorageStatus:
    """Create and mark the root, then validate it.

    Called when the user chooses a storage path and on every start-up. Creating
    the directory is deliberate; formatting or clearing anything is not, and
    Sentinel never does either.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return StorageStatus(
            path=root, available=False, reason=f"Could not create the storage path: {exc}"
        )

    status = check_storage(root, require_marker=False)
    if not status.available:
        return status

    try:
        write_marker(root)
    except OSError as exc:
        return StorageStatus(
            path=root, available=False, reason=f"Storage path is not writable: {exc}"
        )

    logger.info("Storage ready", extra={"path": str(root), "free_bytes": status.free_bytes})
    return status
