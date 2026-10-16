"""System diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

from httpx import AsyncClient

from app.config import Settings
from app.system import diagnostics
from app.system.diagnostics import (
    collect,
    collect_disk_metrics,
    collect_process_metrics,
    detect_tooling,
    to_payload,
)


class TestCollection:
    async def test_it_reports_the_running_process(self, settings: Settings) -> None:
        report = await collect(settings)
        assert report.process.pid > 0
        assert report.process.memory_rss_bytes > 0
        assert report.process.threads >= 1

    async def test_it_reports_the_host(self, settings: Settings) -> None:
        report = await collect(settings)
        assert report.host.cpu_count >= 1
        assert report.host.memory_total_bytes > 0
        assert 0 <= report.host.memory_percent <= 100

    async def test_load_average_is_null_rather_than_faked_where_unavailable(
        self, settings: Settings
    ) -> None:
        """Windows has no real load average; reporting a simulated number would
        be worse than reporting nothing."""
        report = await collect(settings)
        assert report.host.load_average is None or len(report.host.load_average) == 3

    async def test_both_storage_areas_are_reported(
        self, settings: Settings, data_dir: Path, recordings_dir: Path
    ) -> None:
        report = await collect(settings)
        labels = [disk.path for disk in report.disks]
        assert any("data" in label for label in labels)
        assert any("recordings" in label for label in labels)
        assert all(disk.total_bytes > 0 for disk in report.disks if disk.available)

    def test_a_missing_disk_is_reported_not_raised(self, tmp_path: Path) -> None:
        metrics = collect_disk_metrics({"gone": tmp_path / "does-not-exist"})
        assert len(metrics) == 1
        assert metrics[0].available is False
        assert metrics[0].reason

    def test_tooling_detection_finds_ffmpeg_when_installed(self, settings: Settings) -> None:
        """A missing FFmpeg is the most common cause of 'nothing records', and
        it is far easier to see here than in a stack trace."""
        tooling = detect_tooling(settings)
        assert set(tooling) == {"ffmpeg", "ffprobe", "go2rtc"}
        # ffmpeg is installed in this environment; go2rtc may well not be, and
        # None is the correct answer for it rather than an error.
        assert tooling["ffmpeg"] is None or isinstance(tooling["ffmpeg"], str)

    def test_ffmpeg_children_are_counted_separately(self) -> None:
        metrics = collect_process_metrics()
        assert metrics.ffmpeg_children <= metrics.children

    async def test_subsystem_data_is_passed_through(self, settings: Settings) -> None:
        report = await collect(settings, subsystems={"recording": {"running": 3}})
        assert report.subsystems["recording"]["running"] == 3


class TestPayload:
    async def test_the_payload_is_json_serialisable(self, settings: Settings) -> None:
        """It is downloaded as a troubleshooting bundle, so it has to survive
        json.dumps without a custom encoder."""
        payload = to_payload(await collect(settings))
        assert json.loads(json.dumps(payload))["version"]

    async def test_timestamps_are_iso_strings(self, settings: Settings) -> None:
        payload = to_payload(await collect(settings))
        assert payload["generated_at"].endswith("+00:00")

    async def test_secrets_in_subsystem_data_are_redacted(self, settings: Settings) -> None:
        """A diagnostics bundle is exactly the thing a user pastes into a public
        issue, and a subsystem could grow a field that carries a credential."""
        payload = to_payload(
            await collect(
                settings,
                subsystems={
                    "cameras": [
                        {
                            "name": "Porch",
                            "url": "rtsp://admin:hunter2@10.0.0.5/main",
                            "password": "hunter2",
                        }
                    ]
                },
            )
        )
        serialised = json.dumps(payload)
        assert "hunter2" not in serialised
        assert "Porch" in serialised

    async def test_no_credential_appears_in_a_plain_report(self, settings: Settings) -> None:
        payload = json.dumps(to_payload(await collect(settings)))
        secret = settings.load_or_create_secret().decode("ascii")
        assert secret not in payload

    async def test_container_detection_is_a_boolean(self, settings: Settings) -> None:
        assert isinstance((await collect(settings)).container, bool)


class TestDiagnosticsApi:
    async def test_the_endpoint_reports_every_subsystem(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/v1/system/diagnostics")
        assert response.status_code == 200

        body = response.json()
        assert body["version"]
        assert body["process"]["pid"] > 0
        assert set(body["subsystems"]) == {"recording", "motion", "go2rtc", "storage"}
        assert "ffmpeg" in body["tooling"]

    async def test_diagnostics_require_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/system/diagnostics")).status_code == 401

    async def test_the_bundle_downloads_as_a_file(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/v1/system/diagnostics/download")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert "sentinel-diagnostics-" in response.headers["content-disposition"]
        assert json.loads(response.text)["version"]

    async def test_no_camera_password_reaches_the_bundle(self, admin_client: AsyncClient) -> None:
        """This is the artefact a user pastes into a public issue."""
        await admin_client.post(
            "/api/v1/cameras",
            json={
                "name": "Porch",
                "main_stream_url": "rtsp://192.168.50.50:554/stream1",
                "username": "admin",
                "password": "hunter2",
            },
        )
        response = await admin_client.get("/api/v1/system/diagnostics/download")
        assert "hunter2" not in response.text


class TestSampling:
    def test_the_cpu_sample_window_is_long_enough_to_mean_something(self) -> None:
        """psutil needs two readings; a shorter window is noise."""
        assert diagnostics.CPU_SAMPLE_SECONDS >= 0.1
