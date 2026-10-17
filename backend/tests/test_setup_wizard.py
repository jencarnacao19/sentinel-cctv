"""What the first-run setup needs from the server.

The wizard itself lives in the interface. The server remembers whether it was
finished, tells a fresh browser where to go, says whether recordings would land
on the small system disk, and leaves installations from before the wizard alone.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from alembic import command
from app.config import Settings
from app.database.bootstrap import build_alembic_config
from app.storage.root import shares_system_disk
from app.system import address


class TestSetupProgress:
    async def test_a_new_server_is_neither_initialised_nor_set_up(
        self, client: AsyncClient
    ) -> None:
        status = (await client.get("/api/v1/auth/status")).json()
        assert status == {"initialised": False, "setup_completed": False}

    async def test_the_administrator_finishes_it(self, admin_client: AsyncClient) -> None:
        before = (await admin_client.get("/api/v1/auth/status")).json()
        assert before == {"initialised": True, "setup_completed": False}

        finished = await admin_client.post("/api/v1/system/setup/complete")
        assert finished.status_code == 200, finished.text

        after = (await admin_client.get("/api/v1/auth/status")).json()
        assert after == {"initialised": True, "setup_completed": True}
        assert (await admin_client.get("/api/v1/system")).json()["setup_completed"] is True

    async def test_nobody_else_can_finish_it(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/system/setup/complete")).status_code == 401


class TestUpgradingAnExistingInstallation:
    def _users(self, settings: Settings, count: int) -> None:
        with sqlite3.connect(settings.database_path) as connection:
            for index in range(count):
                connection.execute(
                    "INSERT INTO users (id, username, password_hash, is_admin, is_active, "
                    "created_at, updated_at) VALUES (?, ?, 'x', 1, 1, '2026-09-01', '2026-09-01')",
                    (f"user-{index}", f"operator{index}"),
                )

    def _setup_completed(self, settings: Settings) -> str | None:
        with sqlite3.connect(settings.database_path) as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'setup.completed'"
            ).fetchone()
        return row[0] if row else None

    def test_an_installation_with_an_account_is_already_set_up(self, settings: Settings) -> None:
        config = build_alembic_config(settings)
        command.upgrade(config, "0019_motion_event_zones")
        self._users(settings, 1)
        command.upgrade(config, "head")
        assert self._setup_completed(settings) == "true"

    def test_a_fresh_database_still_goes_through_setup(self, settings: Settings) -> None:
        config = build_alembic_config(settings)
        command.upgrade(config, "head")
        assert self._setup_completed(settings) is None


class TestTheSystemDisk:
    def test_two_folders_on_one_filesystem_share_it(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        (tmp_path / "recordings").mkdir()
        assert shares_system_disk(tmp_path / "recordings", tmp_path / "data")

    def test_a_missing_folder_is_not_reported_as_shared(self, tmp_path: Path) -> None:
        assert not shares_system_disk(tmp_path / "missing", tmp_path)

    async def test_the_storage_report_says_so(self, admin_client: AsyncClient) -> None:
        # The test's data and recordings directories sit side by side in one
        # temporary folder, exactly like recordings left on the system disk.
        usage = (await admin_client.get("/api/v1/storage")).json()
        assert usage["shares_system_disk"] is True


def _adapter(name: str, *ips: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        nice_name=name,
        ips=[SimpleNamespace(ip=ip, is_IPv4=True) for ip in ips],
    )


class TestTheAddressInTheLog:
    @pytest.fixture
    def adapters(self, monkeypatch: pytest.MonkeyPatch) -> list[SimpleNamespace]:
        found: list[SimpleNamespace] = []
        monkeypatch.setattr(address.ifaddr, "get_adapters", lambda: found)
        monkeypatch.setattr(address, "_in_container", lambda: False)
        return found

    def test_only_the_household_network_is_offered(self, adapters: list[SimpleNamespace]) -> None:
        adapters += [
            _adapter("lo", "127.0.0.1"),
            _adapter("docker0", "172.17.0.1"),
            _adapter("tailscale0", "100.101.102.103"),
            _adapter("eth1", "10.0.0.7"),
            _adapter("eth0", "192.168.50.50", "169.254.10.1"),
        ]
        assert address.browser_urls(8090) == ["http://192.168.50.50:8090", "http://10.0.0.7:8090"]

    def test_a_bridged_container_does_not_guess(
        self, adapters: list[SimpleNamespace], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(address, "_in_container", lambda: True)
        adapters.append(_adapter("eth0", "172.18.0.2"))
        assert address.browser_urls(8090) == []

    def test_the_log_points_at_setup_until_it_is_finished(
        self, adapters: list[SimpleNamespace], caplog: pytest.LogCaptureFixture
    ) -> None:
        adapters.append(_adapter("eth0", "192.168.50.50"))
        with caplog.at_level(logging.INFO):
            address.announce(8090, setup_completed=False)
            address.announce(8090, setup_completed=True)
        assert [record.getMessage() for record in caplog.records] == [
            "Open http://192.168.50.50:8090 in a browser to set up Sentinel",
            "Sentinel is open at http://192.168.50.50:8090",
        ]

    def test_without_an_address_it_still_says_what_to_do(
        self, adapters: list[SimpleNamespace], caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            address.announce(8090, setup_completed=False)
        assert caplog.records[-1].getMessage() == (
            "Open http://<this server's address>:8090 in a browser to set up Sentinel"
        )
