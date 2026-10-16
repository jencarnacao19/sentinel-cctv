"""FFprobe integration.

The parsing tests are pure. The rest actually invoke FFmpeg and FFprobe against
a generated clip, so a change to the argument list that FFmpeg would reject is
caught here rather than the first time a real camera is added.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.cameras.probe import (
    ProbeError,
    build_probe_command,
    parse_probe_output,
    probe_stream,
)
from app.config import Settings

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="FFmpeg and FFprobe are required for this test"
)


@pytest.fixture(scope="module")
def sample_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A two-second 640x360 H.264 clip with an AAC track."""
    if not FFMPEG:
        pytest.skip("FFmpeg is required")
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(  # noqa: S603 - fixed argument list, no user input
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=25:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return path


class TestCommandBuilding:
    def test_rtsp_urls_get_tcp_transport_and_a_timeout(self) -> None:
        command = build_probe_command(
            "rtsp://10.0.0.5/s1", timeout_seconds=10, ffprobe_binary="ffprobe"
        )
        assert command[0] == "ffprobe"
        assert "-rtsp_transport" in command
        assert command[command.index("-rtsp_transport") + 1] == "tcp"
        assert command[command.index("-timeout") + 1] == "10000000"

    def test_non_rtsp_urls_omit_rtsp_options(self) -> None:
        command = build_probe_command(
            "/media/clip.mp4", timeout_seconds=10, ffprobe_binary="ffprobe"
        )
        assert "-rtsp_transport" not in command

    def test_url_is_a_single_argument(self) -> None:
        """The URL carries the password, so it must never be split or quoted
        into a shell string."""
        url = "rtsp://admin:p@ss word@10.0.0.5/s1"
        command = build_probe_command(url, timeout_seconds=5, ffprobe_binary="ffprobe")
        assert command[-1] == url
        assert command[-2] == "-i"


class TestOutputParsing:
    def test_typical_camera_output(self) -> None:
        result = parse_probe_output(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 2560,
                        "height": 1440,
                        "avg_frame_rate": "25/1",
                        "r_frame_rate": "25/1",
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"bit_rate": "4200000", "duration": "10.5"},
            }
        )
        assert result.codec == "h264"
        assert result.resolution == "2560x1440"
        assert result.fps == 25.0
        assert result.audio_codec == "aac"
        assert result.bitrate_kbps == 4200
        assert result.can_stream_copy is True
        assert result.has_audio is True

    def test_zero_avg_frame_rate_falls_back_to_r_frame_rate(self) -> None:
        result = parse_probe_output(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "avg_frame_rate": "0/0",
                        "r_frame_rate": "20/1",
                    }
                ],
                "format": {},
            }
        )
        assert result.fps == 20.0

    def test_video_only_stream(self) -> None:
        result = parse_probe_output(
            {"streams": [{"codec_type": "video", "codec_name": "h264"}], "format": {}}
        )
        assert result.has_audio is False
        assert result.bitrate_kbps is None

    def test_missing_video_is_reported(self) -> None:
        result = parse_probe_output(
            {"streams": [{"codec_type": "audio", "codec_name": "aac"}], "format": {}}
        )
        assert result.has_video is False

    def test_na_values_are_tolerated(self) -> None:
        result = parse_probe_output(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": "N/A",
                        "avg_frame_rate": "N/A",
                    }
                ],
                "format": {"bit_rate": "N/A", "duration": "N/A"},
            }
        )
        assert result.width is None
        assert result.fps is None
        assert result.bitrate_kbps is None

    def test_empty_payload_does_not_raise(self) -> None:
        assert parse_probe_output({}).has_video is False

    @pytest.mark.parametrize(
        "codec,copyable",
        [("h264", True), ("hevc", True), ("H264", True), ("mpeg4", False), ("vp9", False)],
    )
    def test_stream_copy_support(self, codec: str, copyable: bool) -> None:
        result = parse_probe_output(
            {"streams": [{"codec_type": "video", "codec_name": codec}], "format": {}}
        )
        assert result.can_stream_copy is copyable


@requires_ffmpeg
class TestRealProbe:
    async def test_probes_a_generated_clip(self, settings: Settings, sample_clip: Path) -> None:
        del settings  # activates the temporary install
        result = await probe_stream(str(sample_clip), timeout_seconds=30)
        assert result.codec == "h264"
        assert result.width == 640
        assert result.height == 360
        assert result.fps == 25.0
        assert result.audio_codec == "aac"
        assert result.can_stream_copy is True
        assert result.duration_seconds is not None
        assert 1.5 < result.duration_seconds < 3.0

    async def test_missing_file_raises_a_readable_error(self, settings: Settings) -> None:
        del settings
        with pytest.raises(ProbeError) as exc_info:
            await probe_stream("/definitely/not/here.mp4", timeout_seconds=10)
        assert "could not open" in str(exc_info.value).lower()

    async def test_unreachable_rtsp_host_times_out_and_does_not_hang(
        self, settings: Settings
    ) -> None:
        del settings
        # 198.51.100.0/24 is TEST-NET-2: reserved for documentation and never
        # routed, so this reliably fails rather than reaching a real device.
        with pytest.raises(ProbeError):
            await probe_stream("rtsp://198.51.100.7:554/stream1", timeout_seconds=3)

    async def test_a_credentialled_url_never_appears_in_the_error(self, settings: Settings) -> None:
        del settings
        with pytest.raises(ProbeError) as exc_info:
            await probe_stream("rtsp://admin:hunter2@198.51.100.7:554/s1", timeout_seconds=3)
        assert "hunter2" not in str(exc_info.value)
