"""Known vendor URL patterns and the sequencing/stop-condition logic that
turns a bare host into a list of cameras that actually answered.

``probe_known_patterns`` and ``scan_nvr_channels`` take a ``probe`` callable
precisely so this sequencing logic -- which candidate is tried, in what
order, when a scan gives up -- can be tested deterministically. The real
network path (``probe_stream`` against actual FFprobe) has its own dedicated,
un-mocked coverage in ``test_probe.py``; duplicating that here would only
make these tests slow and flaky without checking anything new.
"""

from __future__ import annotations

import pytest

from app.cameras.probe import ProbeError, ProbeResult
from app.discovery import netsafety, profiles, scanner


def _ok(**overrides: object) -> ProbeResult:
    defaults: dict[str, object] = {"codec": "h264", "width": 1920, "height": 1080, "fps": 25.0}
    defaults.update(overrides)
    return ProbeResult(**defaults)  # type: ignore[arg-type]


class TestPatterns:
    def test_every_documented_vendor_is_present(self) -> None:
        vendors = {pattern.vendor for pattern in profiles.KNOWN_STREAM_PATTERNS}
        assert vendors == {"tapo", "hikvision", "dahua", "reolink"}

    def test_filtering_by_vendor_returns_only_that_vendors_patterns(self) -> None:
        tapo_only = profiles.candidate_patterns("tapo")
        assert {p.vendor for p in tapo_only} == {"tapo"}
        assert len(tapo_only) == 2

    def test_no_vendor_returns_every_pattern(self) -> None:
        assert profiles.candidate_patterns(None) == profiles.KNOWN_STREAM_PATTERNS

    def test_build_url_shape(self) -> None:
        assert (
            profiles.build_url("192.168.50.9", 554, "/stream1") == "rtsp://192.168.50.9:554/stream1"
        )


class TestChannelNumbering:
    def test_hikvision_channel_formula(self) -> None:
        # Channel id is channel*100 + stream, not "16-01" -- getting this wrong
        # probes a different, possibly real, channel rather than failing loudly.
        assert (
            profiles.NVR_CHANNEL_TEMPLATES["hikvision"](1, sub=False) == "/Streaming/Channels/101"
        )
        assert (
            profiles.NVR_CHANNEL_TEMPLATES["hikvision"](16, sub=False) == "/Streaming/Channels/1601"
        )
        assert (
            profiles.NVR_CHANNEL_TEMPLATES["hikvision"](16, sub=True) == "/Streaming/Channels/1602"
        )

    def test_dahua_channel_formula(self) -> None:
        assert (
            profiles.NVR_CHANNEL_TEMPLATES["dahua"](3, sub=False)
            == "/cam/realmonitor?channel=3&subtype=0"
        )
        assert (
            profiles.NVR_CHANNEL_TEMPLATES["dahua"](3, sub=True)
            == "/cam/realmonitor?channel=3&subtype=1"
        )


class TestProbeKnownPatterns:
    async def test_only_answering_patterns_are_returned(self) -> None:
        async def probe(url: str, timeout: float) -> ProbeResult:
            del timeout
            if url.endswith("/stream1"):
                return _ok()
            raise ProbeError("no such stream")

        found = await scanner.probe_known_patterns(
            host="192.168.50.9",
            port=554,
            username=None,
            password=None,
            vendor="tapo",
            probe=probe,
        )
        assert [c.path for c in found] == ["/stream1"]

    async def test_every_pattern_is_tried_even_after_a_hit(self) -> None:
        """A camera commonly serves both a main and a substream; the caller
        wants both offered, not just the first one that answered."""
        seen: list[str] = []

        async def probe(url: str, timeout: float) -> ProbeResult:
            del timeout
            seen.append(url)
            return _ok()

        found = await scanner.probe_known_patterns(
            host="192.168.50.9", port=554, username=None, password=None, vendor="tapo", probe=probe
        )
        assert len(seen) == 2
        assert len(found) == 2

    async def test_credentials_reach_the_probed_url_but_not_the_returned_one(self) -> None:
        captured_urls: list[str] = []

        async def probe(url: str, timeout: float) -> ProbeResult:
            del timeout
            captured_urls.append(url)
            return _ok()

        found = await scanner.probe_known_patterns(
            host="192.168.50.9",
            port=554,
            username="admin",
            password="hunter2",
            vendor="tapo",
            probe=probe,
        )
        assert all("hunter2" in url for url in captured_urls)
        assert all("hunter2" not in candidate.url for candidate in found)

    async def test_a_public_host_is_refused_before_any_probe_runs(self) -> None:
        called = False

        async def probe(url: str, timeout: float) -> ProbeResult:
            nonlocal called
            called = True
            del url, timeout
            return _ok()

        with pytest.raises(netsafety.UnscannableHostError):
            await scanner.probe_known_patterns(
                host="8.8.8.8", port=554, username=None, password=None, vendor=None, probe=probe
            )
        assert called is False


class TestScanNvrChannels:
    async def test_stops_after_a_run_of_consecutive_failures(self) -> None:
        async def probe(url: str, timeout: float) -> ProbeResult:
            del timeout
            # Channels 1-3 answer, then everything else is empty.
            if any(f"channel={n}&" in url for n in (1, 2, 3)):
                return _ok()
            raise ProbeError("timed out")

        result = await scanner.scan_nvr_channels(
            host="192.168.50.100",
            port=554,
            username=None,
            password=None,
            vendor="dahua",
            max_channels=16,
            max_consecutive_failures=2,
            probe=probe,
        )
        assert [c.channel for c in result.channels] == [1, 2, 3]
        # 3 successes + 2 failures before giving up = 5 channels actually checked.
        assert result.checked == 5
        assert "2 channels in a row" in result.stopped_reason

    async def test_a_gap_does_not_reset_the_failure_counter_incorrectly(self) -> None:
        """Channel 2 fails, channel 3 answers, resetting the streak; only a
        run at the very end should stop the scan."""

        async def probe(url: str, timeout: float) -> ProbeResult:
            del timeout
            if "channel=2&" in url:
                raise ProbeError("timed out")
            return _ok()

        result = await scanner.scan_nvr_channels(
            host="192.168.50.100",
            port=554,
            username=None,
            password=None,
            vendor="dahua",
            max_channels=4,
            max_consecutive_failures=2,
            probe=probe,
        )
        assert [c.channel for c in result.channels] == [1, 3, 4]
        assert result.checked == 4
        assert result.stopped_reason == "Reached the channel limit."

    async def test_reaching_the_channel_limit_without_failing_is_reported_distinctly(self) -> None:
        async def probe(url: str, timeout: float) -> ProbeResult:
            del url, timeout
            return _ok()

        result = await scanner.scan_nvr_channels(
            host="192.168.50.100",
            port=554,
            username=None,
            password=None,
            vendor="hikvision",
            max_channels=4,
            probe=probe,
        )
        assert result.checked == 4
        assert result.stopped_reason == "Reached the channel limit."

    async def test_unknown_vendor_is_rejected(self) -> None:
        async def probe(url: str, timeout: float) -> ProbeResult:
            del url, timeout
            return _ok()

        with pytest.raises(ValueError, match="No channel numbering pattern"):
            await scanner.scan_nvr_channels(
                host="192.168.50.100",
                port=554,
                username=None,
                password=None,
                vendor="not-a-real-vendor",
                probe=probe,
            )

    async def test_channel_count_is_capped_regardless_of_caller_input(self) -> None:
        async def probe(url: str, timeout: float) -> ProbeResult:
            del url, timeout
            return _ok()

        with pytest.raises(ValueError, match="Cannot scan more than"):
            await scanner.scan_nvr_channels(
                host="192.168.50.100",
                port=554,
                username=None,
                password=None,
                vendor="hikvision",
                max_channels=scanner.MAX_CHANNELS + 1,
                probe=probe,
            )

    async def test_a_public_host_is_refused_before_any_channel_is_probed(self) -> None:
        called = False

        async def probe(url: str, timeout: float) -> ProbeResult:
            nonlocal called
            called = True
            del url, timeout
            return _ok()

        with pytest.raises(netsafety.UnscannableHostError):
            await scanner.scan_nvr_channels(
                host="203.0.113.9",
                port=554,
                username=None,
                password=None,
                vendor="hikvision",
                probe=probe,
            )
        assert called is False

    async def test_start_channel_offsets_the_sequence(self) -> None:
        seen: list[int] = []

        async def probe(url: str, timeout: float) -> ProbeResult:
            del url, timeout
            return _ok()

        result = await scanner.scan_nvr_channels(
            host="192.168.50.100",
            port=554,
            username=None,
            password=None,
            vendor="hikvision",
            start_channel=9,
            max_channels=3,
            probe=probe,
        )
        seen = [c.channel for c in result.channels]
        assert seen == [9, 10, 11]
