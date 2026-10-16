"""Turning a host and a set of URL guesses into cameras that actually answer.

Two flows share the same shape: try a sequence of candidate stream URLs
against a host, using the exact FFprobe path a saved camera is tested with
(``app.cameras.probe.probe_stream``), and keep only the ones that answered.
Both take a ``probe`` callable defaulting to the real prober -- tests use that
seam to exercise the sequencing and stop-condition logic here deterministically
without needing a real camera to answer; the network path itself
(``probe_stream`` against real FFprobe) already has its own dedicated,
un-mocked test coverage in ``tests/test_probe.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.cameras.probe import ProbeError, ProbeResult, probe_stream
from app.cameras.rtsp import build_stream_url
from app.discovery import netsafety, profiles
from app.logging_config import get_logger

logger = get_logger("DISCOVERY")

#: Signature every probe implementation (real or injected for a test) follows.
ProbeFn = Callable[[str, float], Awaitable[ProbeResult]]

MAX_CHANNELS = 64
MAX_CONSECUTIVE_FAILURES = 10


async def _default_probe(url: str, timeout_seconds: float) -> ProbeResult:
    return await probe_stream(url, timeout_seconds=timeout_seconds)


@dataclass(frozen=True, slots=True)
class CandidateResult:
    vendor: str
    label: str
    path: str
    url: str
    """Never carries credentials -- the caller already has them."""
    result: ProbeResult


async def probe_known_patterns(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    vendor: str | None,
    timeout_seconds: float = 6.0,
    probe: ProbeFn = _default_probe,
) -> list[CandidateResult]:
    """Try every known URL pattern for ``vendor`` (or all of them) against
    ``host`` and return only the ones that actually opened.

    Every candidate is tried regardless of whether an earlier one succeeded:
    a camera commonly serves both a main and a substream, and the caller
    wants both offered, not just the first hit.
    """
    await netsafety.ensure_scannable_host(host)

    found: list[CandidateResult] = []
    for pattern in profiles.candidate_patterns(vendor):
        sanitised = profiles.build_url(host, port, pattern.path)
        live_url = build_stream_url(sanitised, username, password)
        try:
            result = await probe(live_url, timeout_seconds)
        except ProbeError as exc:
            logger.debug(
                "Known pattern did not answer",
                extra={"vendor": pattern.vendor, "path": pattern.path, "error": str(exc)},
            )
            continue
        found.append(
            CandidateResult(
                vendor=pattern.vendor,
                label=pattern.label,
                path=pattern.path,
                url=sanitised,
                result=result,
            )
        )
    return found


@dataclass(frozen=True, slots=True)
class ChannelResult:
    channel: int
    path: str
    url: str
    """Never carries credentials."""
    result: ProbeResult


@dataclass(frozen=True, slots=True)
class NvrScanResult:
    channels: list[ChannelResult]
    checked: int
    stopped_reason: str


async def scan_nvr_channels(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    vendor: str,
    sub: bool = False,
    start_channel: int = 1,
    max_channels: int = 16,
    max_consecutive_failures: int = 2,
    timeout_seconds: float = 6.0,
    probe: ProbeFn = _default_probe,
) -> NvrScanResult:
    """Probe channels ``start_channel .. start_channel + max_channels - 1`` in
    sequence, stopping early after ``max_consecutive_failures`` in a row.

    Sequential and bounded on purpose: probing an unbounded, unauthenticated
    range of paths against one address as fast as possible is what a port
    scanner does, and a box that happens to have sixty-four channels
    configured but only eight cameras plugged in would otherwise cost sixty-
    four full timeouts for no benefit -- stopping after a short run of
    failures assumes the rest are empty too, which matches how these boxes are
    actually populated in practice (channels filled from 1 upward).
    """
    if max_channels > MAX_CHANNELS:
        raise ValueError(f"Cannot scan more than {MAX_CHANNELS} channels in one request.")
    if max_consecutive_failures > MAX_CONSECUTIVE_FAILURES:
        raise ValueError(f"Cannot allow more than {MAX_CONSECUTIVE_FAILURES} consecutive failures.")
    template = profiles.NVR_CHANNEL_TEMPLATES.get(vendor)
    if template is None:
        known = ", ".join(profiles.NVR_VENDORS)
        raise ValueError(f"No channel numbering pattern known for '{vendor}'. Known: {known}.")

    await netsafety.ensure_scannable_host(host)

    channels: list[ChannelResult] = []
    consecutive_failures = 0
    checked = 0
    stopped_reason = "Reached the channel limit."

    for channel in range(start_channel, start_channel + max_channels):
        checked += 1
        path = template(channel, sub=sub)
        sanitised = profiles.build_url(host, port, path)
        live_url = build_stream_url(sanitised, username, password)
        try:
            result = await probe(live_url, timeout_seconds)
        except ProbeError as exc:
            consecutive_failures += 1
            logger.debug(
                "NVR channel did not answer", extra={"channel": channel, "error": str(exc)}
            )
            if consecutive_failures >= max_consecutive_failures:
                stopped_reason = (
                    f"Stopped after {consecutive_failures} channels in a row did not answer."
                )
                break
            continue

        consecutive_failures = 0
        channels.append(ChannelResult(channel=channel, path=path, url=sanitised, result=result))

    return NvrScanResult(channels=channels, checked=checked, stopped_reason=stopped_reason)
