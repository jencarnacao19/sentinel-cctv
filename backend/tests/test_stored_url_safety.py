"""Stored camera addresses are checked where they are used, not only at the API.

The API only accepts RTSP stream URLs, but the database can also arrive from a
backup file, which can be edited by hand. A stream URL such as ``exec:...``
written into go2rtc's configuration would run a command inside the container.
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.cameras.ptz import PtzManager
from app.cameras.service import resolve_stream_url
from app.config import Settings
from app.streaming.config import build_streams
from app.system.backup import DATABASE_NAME, BackupError, create_backup, restore_backup
from tests.test_streaming import make_camera

CAMERA_PAYLOAD = {
    "name": "Front door",
    "main_stream_url": "rtsp://192.168.50.50:554/stream1",
    "username": "admin",
    "password": "hunter2",
}
COMMAND_SOURCE = "exec:/bin/sh -c id"


class TestStoredStreamUrls:
    @pytest.mark.parametrize(
        "stored", [COMMAND_SOURCE, "echo:/bin/id", "ffmpeg:file:/data/config/secret", "file:/x"]
    )
    def test_a_non_rtsp_url_is_never_handed_out(self, stored: str) -> None:
        camera = make_camera(main_stream_url=stored, sub_stream_url=stored)
        assert resolve_stream_url(camera) is None
        assert resolve_stream_url(camera, sub=True) is None

    def test_go2rtc_never_receives_it(self) -> None:
        good = make_camera()
        bad = make_camera(id="99999999-8888-7777-6666-555555555555", main_stream_url=COMMAND_SOURCE)
        streams = build_streams([good, bad])
        assert len(streams) == 1
        assert not any("exec:" in source for sources in streams.values() for source in sources)

    def test_a_normal_rtsp_url_still_works(self) -> None:
        camera = make_camera(main_stream_url="rtsps://192.168.50.50:322/stream1")
        assert resolve_stream_url(camera) == "rtsps://192.168.50.50:322/stream1"


class TestStoredPtzAddress:
    def test_a_stored_address_on_another_host_is_pinned_to_the_camera(self) -> None:
        camera = make_camera(
            ptz_supported=True,
            onvif_ptz_xaddr="http://127.0.0.1:1984/api/exit",
            onvif_profile_token="profile_1",
            encrypted_tapo_password=None,
        )
        target = PtzManager._target(camera)
        assert target.xaddr == "http://192.168.50.50:1984/api/exit"


def _tamper(
    archive_path: Path, destination: Path, statement: str, params: tuple[str, ...] = ()
) -> Path:
    """Copy ``archive_path`` with ``statement`` applied to its database."""
    work = destination.parent / "tamper.db"
    with zipfile.ZipFile(archive_path) as archive:
        work.write_bytes(archive.read(DATABASE_NAME))
        others = {name: archive.read(name) for name in archive.namelist() if name != DATABASE_NAME}
    connection = sqlite3.connect(work)
    try:
        connection.execute(statement, params)
        connection.commit()
    finally:
        connection.close()
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in others.items():
            archive.writestr(name, data)
        archive.write(work, DATABASE_NAME)
    return destination


class TestTamperedBackup:
    async def test_a_backup_with_a_command_as_stream_url_is_refused(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        response = await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        assert response.status_code == 201, response.text
        original = tmp_path / "backup.zip"
        await create_backup(settings, original)
        tampered = _tamper(
            original,
            tmp_path / "tampered.zip",
            "UPDATE cameras SET main_stream_url = ?",
            (COMMAND_SOURCE,),
        )

        with pytest.raises(BackupError, match="not RTSP"):
            await restore_backup(settings, tampered)

        connection = sqlite3.connect(settings.database_path)
        try:
            urls = connection.execute("SELECT main_stream_url FROM cameras").fetchall()
        finally:
            connection.close()
        assert urls == [("rtsp://192.168.50.50:554/stream1",)]

    async def test_an_untouched_backup_still_restores(
        self, admin_client: AsyncClient, settings: Settings, tmp_path: Path
    ) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        archive = tmp_path / "backup.zip"
        await create_backup(settings, archive)
        await restore_backup(settings, archive)  # must not raise
