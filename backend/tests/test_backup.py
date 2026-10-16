"""Configuration backup and restore."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.system import backup
from app.system import router as system_router
from app.system.backup import (
    DATABASE_NAME,
    MANIFEST_NAME,
    BackupError,
    create_backup,
    read_manifest,
    restore_backup,
    suggested_filename,
)

CAMERA_PAYLOAD = {
    "name": "Entrada",
    "main_stream_url": "rtsp://192.168.1.50:554/stream1",
    "username": "admin",
    "password": "hunter2",
}


async def make_archive(admin_client: AsyncClient, settings: Settings, tmp_path: Path) -> Path:
    await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
    destination = tmp_path / "backup.zip"
    await create_backup(settings, destination)
    return destination


class TestBackupContents:
    async def test_the_archive_contains_a_manifest_and_a_database(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        archive_path = await make_archive(admin_client, settings, tmp_path)

        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
        assert MANIFEST_NAME in names
        assert DATABASE_NAME in names

    async def test_camera_passwords_are_not_in_the_archive(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        """They are encrypted with a secret that stays on the original machine,
        so including them would be either useless or dangerous."""
        archive_path = await make_archive(admin_client, settings, tmp_path)

        raw = archive_path.read_bytes()
        assert b"hunter2" not in raw

        with zipfile.ZipFile(archive_path) as archive:
            extracted = tmp_path / "extracted.db"
            extracted.write_bytes(archive.read(DATABASE_NAME))

        connection = sqlite3.connect(extracted)
        try:
            stored = connection.execute(
                "SELECT username, encrypted_password FROM cameras"
            ).fetchall()
        finally:
            connection.close()

        assert stored, "the camera should still be in the backup"
        assert stored[0][0] == "admin", "the username is configuration, not a secret"
        assert stored[0][1] is None, "the password must have been stripped"

    async def test_the_install_secret_is_not_in_the_archive(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        secret = settings.load_or_create_secret()
        archive_path = await make_archive(admin_client, settings, tmp_path)
        assert secret not in archive_path.read_bytes()

    async def test_user_password_hashes_survive(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        """They are one-way hashes, which is the point of storing them that way,
        and keeping them means the administrator account survives a restore."""
        archive_path = await make_archive(admin_client, settings, tmp_path)

        with zipfile.ZipFile(archive_path) as archive:
            extracted = tmp_path / "users.db"
            extracted.write_bytes(archive.read(DATABASE_NAME))

        connection = sqlite3.connect(extracted)
        try:
            rows = connection.execute("SELECT username, password_hash FROM users").fetchall()
        finally:
            connection.close()
        assert rows
        assert rows[0][1].startswith("$argon2id$")

    async def test_the_manifest_says_passwords_are_excluded(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        archive_path = await make_archive(admin_client, settings, tmp_path)
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read(MANIFEST_NAME))

        assert manifest["includes_camera_passwords"] is False
        assert "entered again" in manifest["note"]
        assert manifest["camera_count"] == 1
        assert manifest["schema_revision"]

    async def test_recordings_are_never_included(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path, recordings_dir: Path
    ) -> None:
        footage = recordings_dir / "cam" / "clip.mp4"
        footage.parent.mkdir(parents=True)
        footage.write_bytes(b"\x00" * 4096)

        archive_path = await make_archive(admin_client, settings, tmp_path)
        with zipfile.ZipFile(archive_path) as archive:
            assert not any(name.endswith(".mp4") for name in archive.namelist())

    async def test_a_backup_of_a_running_install_is_consistent(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        """The live database is in WAL mode, so the .db file alone is not a
        complete database; the snapshot must still be readable."""
        archive_path = await make_archive(admin_client, settings, tmp_path)
        with zipfile.ZipFile(archive_path) as archive:
            extracted = tmp_path / "consistent.db"
            extracted.write_bytes(archive.read(DATABASE_NAME))

        connection = sqlite3.connect(extracted)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()


class TestManifestValidation:
    async def test_a_valid_archive_is_described(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        archive_path = await make_archive(admin_client, settings, tmp_path)
        manifest = read_manifest(archive_path)
        assert manifest.camera_count == 1
        assert manifest.includes_camera_passwords is False

    def test_a_file_that_is_not_an_archive_is_refused(self, tmp_path: Path) -> None:
        bogus = tmp_path / "notes.txt"
        bogus.write_text("hello")
        with pytest.raises(BackupError, match="readable archive"):
            read_manifest(bogus)

    def test_an_archive_without_a_manifest_is_refused(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "other.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("something.txt", "not ours")
        with pytest.raises(BackupError, match="manifest"):
            read_manifest(archive_path)

    def test_a_newer_format_is_refused_rather_than_half_applied(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "future.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(
                    {
                        "format_version": backup.BACKUP_FORMAT_VERSION + 1,
                        "app_version": "9.9.9",
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                ),
            )
        with pytest.raises(BackupError, match="newer version"):
            read_manifest(archive_path)

    def test_an_archive_with_traversal_paths_is_refused(self, tmp_path: Path) -> None:
        """A restore extracts attacker-controllable names."""
        archive_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../../etc/passwd", "root:x:0:0")
            archive.writestr(MANIFEST_NAME, "{}")
        with pytest.raises(BackupError, match="unsafe file paths"):
            read_manifest(archive_path)

    def test_a_corrupt_manifest_is_refused(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "broken.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(MANIFEST_NAME, "{not json")
        with pytest.raises(BackupError):
            read_manifest(archive_path)


class TestRestore:
    async def test_a_restore_brings_the_cameras_back(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        archive_path = await make_archive(admin_client, settings, tmp_path)

        # Simulate a rebuilt machine: the database is replaced by the backup.
        await restore_backup(settings, archive_path)

        connection = sqlite3.connect(settings.database_path)
        try:
            names = connection.execute("SELECT name FROM cameras").fetchall()
        finally:
            connection.close()
        assert [row[0] for row in names] == ["Entrada"]

    async def test_the_previous_database_is_kept_aside(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        """Restoring the wrong archive must be recoverable."""
        archive_path = await make_archive(admin_client, settings, tmp_path)
        await restore_backup(settings, archive_path)

        kept = list(settings.database_path.parent.glob("*-replaced-*.db"))
        assert len(kept) == 1
        assert kept[0].stat().st_size > 0

    async def test_the_restored_database_is_intact(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        """The contents are replaced in place rather than the file swapped, so
        the WAL side files must still agree with the database afterwards."""
        archive_path = await make_archive(admin_client, settings, tmp_path)
        await restore_backup(settings, archive_path)

        connection = sqlite3.connect(settings.database_path)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        finally:
            connection.close()

    async def test_a_restore_works_while_the_database_is_open(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        """The running application holds the database open. Replacing the file
        would fail on Windows and be silently ineffective on Linux."""
        archive_path = await make_archive(admin_client, settings, tmp_path)

        held = sqlite3.connect(settings.database_path)
        try:
            held.execute("SELECT COUNT(*) FROM cameras").fetchone()
            await restore_backup(settings, archive_path)
        finally:
            held.close()

        connection = sqlite3.connect(settings.database_path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 1
        finally:
            connection.close()

    async def test_a_backup_containing_a_bad_database_is_refused(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """The live database must not be replaced by something unreadable."""
        archive_path = tmp_path / "bad.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(
                    {
                        "format_version": backup.BACKUP_FORMAT_VERSION,
                        "app_version": "0.1.0",
                        "created_at": datetime.now(UTC).isoformat(),
                        "camera_count": 0,
                        "user_count": 0,
                        "includes_camera_passwords": False,
                    }
                ),
            )
            archive.writestr(DATABASE_NAME, b"this is not a database")

        with pytest.raises(BackupError, match="not readable"):
            await restore_backup(settings, archive_path)

    async def test_a_backup_with_no_database_is_refused(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        archive_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(
                    {
                        "format_version": backup.BACKUP_FORMAT_VERSION,
                        "app_version": "0.1.0",
                        "created_at": datetime.now(UTC).isoformat(),
                        "camera_count": 0,
                        "user_count": 0,
                        "includes_camera_passwords": False,
                    }
                ),
            )
        with pytest.raises(BackupError, match="no database"):
            await restore_backup(settings, archive_path)


class TestBackupApi:
    async def test_a_backup_can_be_downloaded(self, admin_client: AsyncClient) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        response = await admin_client.post("/api/v1/system/backup")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "sentinel-backup-" in response.headers["content-disposition"]
        assert b"hunter2" not in response.content
        assert response.content[:2] == b"PK"

    async def test_download_removes_its_temporary_archive(
        self,
        admin_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        created: list[Path] = []
        real_named_temporary_file = tempfile.NamedTemporaryFile

        def tracked_named_temporary_file(*, suffix: str, delete: bool) -> object:
            handle = real_named_temporary_file(suffix=suffix, delete=delete, dir=tmp_path)
            created.append(Path(handle.name))
            return handle

        monkeypatch.setattr(
            system_router.tempfile, "NamedTemporaryFile", tracked_named_temporary_file
        )

        response = await admin_client.post("/api/v1/system/backup")

        assert response.status_code == 200
        assert len(created) == 1
        assert not created[0].exists()

    async def test_backup_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/system/backup")).status_code == 401

    async def test_an_archive_can_be_inspected_before_it_is_applied(
        self, admin_client: AsyncClient
    ) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        archive = (await admin_client.post("/api/v1/system/backup")).content

        response = await admin_client.post(
            "/api/v1/system/backup/inspect",
            files={"file": ("backup.zip", archive, "application/zip")},
        )
        assert response.status_code == 200
        assert response.json()["camera_count"] == 1
        assert response.json()["includes_camera_passwords"] is False

    async def test_inspecting_a_non_backup_is_a_clear_error(
        self, admin_client: AsyncClient
    ) -> None:
        response = await admin_client.post(
            "/api/v1/system/backup/inspect",
            files={"file": ("notes.txt", b"just some text", "text/plain")},
        )
        assert response.status_code == 422
        assert "archive" in response.json()["detail"].lower()

    async def test_restoring_reports_that_a_restart_is_needed(
        self, admin_client: AsyncClient
    ) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        archive = (await admin_client.post("/api/v1/system/backup")).content

        response = await admin_client.post(
            "/api/v1/system/backup/restore",
            files={"file": ("backup.zip", archive, "application/zip")},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["restart_required"] is True
        assert "password" in body["message"].lower()

    async def test_restoring_rubbish_leaves_the_installation_alone(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        before = settings.database_path.stat().st_size

        response = await admin_client.post(
            "/api/v1/system/backup/restore",
            files={"file": ("evil.zip", b"not a zip at all", "application/zip")},
        )
        assert response.status_code == 422
        assert settings.database_path.exists()
        assert settings.database_path.stat().st_size == before


class TestNaming:
    def test_the_suggested_filename_sorts_chronologically(self) -> None:
        earlier = suggested_filename(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
        later = suggested_filename(datetime(2026, 8, 13, 15, 32, 0, tzinfo=UTC))
        assert earlier < later
        assert earlier.endswith(".zip")
        assert earlier.startswith("sentinel-backup-")
