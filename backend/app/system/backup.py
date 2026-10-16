"""Configuration backup and restore.

What a backup is for: getting a rebuilt machine back to the same cameras,
retention policy, zones and users without re-doing the setup by hand.

**Camera passwords are deliberately not included.** They are encrypted with a key
derived from an install secret that lives only on the original machine, so a
backup containing them would either be useless (without the secret) or dangerous
(with it, in the same archive). The product specification is explicit that
security wins over convenience here: camera passwords must be re-entered after a
restore, and the archive says so in its manifest.

Recordings are never included either. They are the bulk of the data, they are
already on dedicated storage, and a backup that took hours would never be run.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import __version__
from app.config import Settings
from app.database.session import dispose_engine
from app.logging_config import get_logger

logger = get_logger("SYSTEM")

MANIFEST_NAME = "sentinel-backup.json"
DATABASE_NAME = "database/sentinel.db"

#: Bumped when the archive layout changes in a way an older Sentinel could not
#: read. The application version alone is not enough: most releases do not
#: change the format at all.
BACKUP_FORMAT_VERSION = 1

#: A configuration backup is kilobytes. Anything near this is not one of ours.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


class BackupError(RuntimeError):
    """Backup or restore failed. The message is safe to show a user."""


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_version: int
    app_version: str
    created_at: datetime
    schema_revision: str | None
    camera_count: int
    user_count: int
    includes_camera_passwords: bool
    """Always False. Present so a future format cannot silently change the
    meaning of an old archive."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "app_version": self.app_version,
            "created_at": self.created_at.isoformat(),
            "schema_revision": self.schema_revision,
            "camera_count": self.camera_count,
            "user_count": self.user_count,
            "includes_camera_passwords": self.includes_camera_passwords,
            "note": (
                "Camera passwords are not included. They are encrypted with a "
                "secret that stays on the original machine, so they must be "
                "entered again after restoring."
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackupManifest:
        try:
            return cls(
                format_version=int(data["format_version"]),
                app_version=str(data["app_version"]),
                created_at=datetime.fromisoformat(str(data["created_at"])),
                schema_revision=data.get("schema_revision"),
                camera_count=int(data.get("camera_count", 0)),
                user_count=int(data.get("user_count", 0)),
                includes_camera_passwords=bool(data.get("includes_camera_passwords", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupError("This file is not a valid Sentinel backup.") from exc


def _snapshot_database(source: Path, destination: Path) -> None:
    """Take a consistent copy of the live database.

    ``VACUUM INTO`` is used rather than copying the file: with WAL journalling
    the ``.db`` on its own is not a complete database, and copying it while the
    recorder is writing would produce a torn snapshot.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(source)
    try:
        connection.execute("VACUUM INTO ?", (str(destination),))
    except sqlite3.Error as exc:
        raise BackupError(f"Could not read the database: {exc}") from exc
    finally:
        connection.close()


def _copy_database(source: Path, destination: Path) -> None:
    """Replace ``destination``'s contents with ``source``'s, in place."""
    origin = sqlite3.connect(source)
    target = sqlite3.connect(destination)
    try:
        origin.backup(target)
    except sqlite3.Error as exc:
        raise BackupError(f"Could not apply the backup: {exc}") from exc
    finally:
        origin.close()
        target.close()


def _strip_secrets(database: Path) -> None:
    """Remove everything that must not leave the machine.

    Camera passwords go. User password hashes and API-token hashes stay: they are
    one-way hashes, which is the entire point of storing them that way, and
    keeping them means the administrator account survives a restore.
    """
    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE cameras SET encrypted_password = NULL")
        connection.commit()
    except sqlite3.Error as exc:
        raise BackupError(f"Could not sanitise the backup: {exc}") from exc
    finally:
        connection.close()


def _read_counts(database: Path) -> tuple[int, int, str | None]:
    connection = sqlite3.connect(database)
    try:
        cameras = connection.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
        users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        revision_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        revision = revision_row[0] if revision_row else None
    except sqlite3.Error:
        return 0, 0, None
    finally:
        connection.close()
    return int(cameras), int(users), revision


def _build_archive(settings: Settings, destination: Path) -> BackupManifest:
    """Blocking; always called through a thread."""
    if not settings.database_path.exists():
        raise BackupError("There is no database to back up yet.")

    with tempfile.TemporaryDirectory(prefix="sentinel-backup-") as work_dir:
        staged = Path(work_dir) / "sentinel.db"
        _snapshot_database(settings.database_path, staged)
        _strip_secrets(staged)
        camera_count, user_count, revision = _read_counts(staged)

        manifest = BackupManifest(
            format_version=BACKUP_FORMAT_VERSION,
            app_version=__version__,
            created_at=datetime.now(UTC),
            schema_revision=revision,
            camera_count=camera_count,
            user_count=user_count,
            includes_camera_passwords=False,
        )

        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                MANIFEST_NAME, json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False)
            )
            archive.write(staged, DATABASE_NAME)

    logger.info(
        "Backup created",
        extra={"cameras": camera_count, "users": user_count, "revision": revision},
    )
    return manifest


async def create_backup(settings: Settings, destination: Path) -> BackupManifest:
    return await asyncio.to_thread(_build_archive, settings, destination)


def suggested_filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"sentinel-backup-{stamp}.zip"


# --- Restore ------------------------------------------------------------


def read_manifest(archive_path: Path) -> BackupManifest:
    """Inspect an archive without applying it."""
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise BackupError("That file is far too large to be a Sentinel backup.")

    try:
        with zipfile.ZipFile(archive_path) as archive:
            _assert_safe_members(archive)
            try:
                raw = archive.read(MANIFEST_NAME)
            except KeyError as exc:
                raise BackupError("This archive has no Sentinel backup manifest.") from exc
    except zipfile.BadZipFile as exc:
        raise BackupError("This file is not a readable archive.") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupError("The backup manifest is unreadable.") from exc
    if not isinstance(data, dict):
        raise BackupError("The backup manifest is unreadable.")

    manifest = BackupManifest.from_dict(data)
    if manifest.format_version > BACKUP_FORMAT_VERSION:
        raise BackupError(
            f"This backup was made by a newer version of Sentinel "
            f"(format {manifest.format_version}). Update Sentinel first."
        )
    return manifest


def _assert_safe_members(archive: zipfile.ZipFile) -> None:
    """Refuse an archive that would write outside the target directory.

    A restore extracts attacker-controllable names; ``../`` or an absolute path
    inside a zip is the classic path-traversal vector.
    """
    for name in archive.namelist():
        if name.startswith(("/", "\\")) or ".." in Path(name).parts:
            raise BackupError("This archive contains unsafe file paths and was refused.")
        if len(name) > 512:
            raise BackupError("This archive contains an implausible file path.")


def _apply(settings: Settings, archive_path: Path) -> BackupManifest:
    """Blocking; always called through a thread."""
    manifest = read_manifest(archive_path)

    with tempfile.TemporaryDirectory(prefix="sentinel-restore-") as work_dir:
        staged = Path(work_dir) / "sentinel.db"
        with zipfile.ZipFile(archive_path) as archive:
            _assert_safe_members(archive)
            try:
                with archive.open(DATABASE_NAME) as source, staged.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            except KeyError as exc:
                raise BackupError("This backup contains no database.") from exc

        # Prove it is a real database before touching the live one. Restoring a
        # corrupt file would take the installation down with no way back.
        try:
            probe = sqlite3.connect(staged)
            try:
                probe.execute("SELECT COUNT(*) FROM cameras").fetchone()
                probe.execute("SELECT version_num FROM alembic_version").fetchone()
            finally:
                probe.close()
        except sqlite3.Error as exc:
            raise BackupError("The database inside this backup is not readable.") from exc

        target_path = settings.database_path
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # The current database is kept alongside rather than deleted, so a
        # restore that turns out to be the wrong archive is recoverable.
        if target_path.exists():
            aside = target_path.with_name(
                f"{target_path.stem}-replaced-{datetime.now(UTC):%Y%m%d-%H%M%S}.db"
            )
            _snapshot_database(target_path, aside)
            logger.warning(
                "Previous database kept aside before restore", extra={"kept_as": aside.name}
            )

        # SQLite's online backup API replaces the *contents* of the live
        # database rather than the file. Moving a new file into place would fail
        # outright on Windows, where an open file cannot be unlinked, and would
        # be quietly wrong on Linux, where the move succeeds but every existing
        # connection keeps reading the old inode -- so the restore would appear
        # to work and change nothing. This path also leaves the WAL side files
        # consistent, which a file swap would not.
        _copy_database(staged, target_path)

    logger.warning(
        "Configuration restored from backup",
        extra={"cameras": manifest.camera_count, "revision": manifest.schema_revision},
    )
    return manifest


async def restore_backup(settings: Settings, archive_path: Path) -> BackupManifest:
    """Replace the configuration database with the one in ``archive_path``.

    The engine is disposed first, which matters on both platforms for different
    reasons: Windows refuses to move a file that is still open, and on Linux the
    move would succeed while every pooled connection kept using the old inode --
    so the restore would appear to work and change nothing.

    Sentinel still has to be restarted afterwards: the recorders, motion workers
    and go2rtc configuration were all derived from the previous camera list.
    """
    # Validate before closing anything. A bad archive must not cost the caller
    # its database connections for nothing.
    read_manifest(archive_path)

    await dispose_engine()
    return await asyncio.to_thread(_apply, settings, archive_path)
