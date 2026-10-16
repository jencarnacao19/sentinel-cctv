"""Known camera and NVR URL patterns, for devices that do not speak ONVIF.

Every path here is a *candidate*, never a claim: even within one product line,
firmware revisions move things around. Nothing in this module is shown to a
user directly -- ``app.discovery.scanner`` runs each candidate through the
same FFprobe path a saved camera is tested with
(``app.cameras.probe.probe_stream``) before it is ever offered, so what
reaches the API is exactly what answered, not what was merely guessed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StreamPattern:
    vendor: str
    label: str
    path: str


#: Documented (or widely reported) default RTSP paths for consumer cameras
#: whose ONVIF support is absent, disabled by default, or locked behind a
#: vendor app.
KNOWN_STREAM_PATTERNS: tuple[StreamPattern, ...] = (
    StreamPattern("tapo", "Main stream", "/stream1"),
    StreamPattern("tapo", "Sub stream", "/stream2"),
    StreamPattern("hikvision", "Channel 1 main", "/Streaming/Channels/101"),
    StreamPattern("hikvision", "Channel 1 sub", "/Streaming/Channels/102"),
    StreamPattern("dahua", "Main stream", "/cam/realmonitor?channel=1&subtype=0"),
    StreamPattern("dahua", "Sub stream", "/cam/realmonitor?channel=1&subtype=1"),
    StreamPattern("reolink", "Main stream", "/h264Preview_01_main"),
    StreamPattern("reolink", "Sub stream", "/h264Preview_01_sub"),
)

#: Vendor identifiers in first-seen order, for populating a picker.
KNOWN_VENDORS: tuple[str, ...] = tuple(
    dict.fromkeys(pattern.vendor for pattern in KNOWN_STREAM_PATTERNS)
)


def candidate_patterns(vendor: str | None) -> tuple[StreamPattern, ...]:
    """Patterns worth trying. Every one of them when ``vendor`` is unset --
    a user who does not know their camera's brand should not have to."""
    if vendor is None:
        return KNOWN_STREAM_PATTERNS
    return tuple(pattern for pattern in KNOWN_STREAM_PATTERNS if pattern.vendor == vendor)


def build_url(host: str, port: int, path: str) -> str:
    return f"rtsp://{host}:{port}{path}"


# --- NVR multi-channel numbering ------------------------------------------

ChannelPathBuilder = Callable[..., str]


def _hikvision_channel_path(channel: int, *, sub: bool) -> str:
    # Channel id is `channel * 100 + stream`, e.g. channel 16 main is 1601 --
    # not zero-padded "16-01". Getting this formula wrong does not fail
    # loudly; it silently probes a different, possibly real, channel.
    stream = 2 if sub else 1
    return f"/Streaming/Channels/{channel * 100 + stream}"


def _dahua_channel_path(channel: int, *, sub: bool) -> str:
    return f"/cam/realmonitor?channel={channel}&subtype={1 if sub else 0}"


#: Many budget NVRs and their rebrands clone Hikvision's channel numbering
#: exactly (shared firmware base); Dahua's is common enough on its own to
#: list separately rather than assume it always matches.
NVR_CHANNEL_TEMPLATES: dict[str, ChannelPathBuilder] = {
    "hikvision": _hikvision_channel_path,
    "dahua": _dahua_channel_path,
}

NVR_VENDORS: tuple[str, ...] = tuple(NVR_CHANNEL_TEMPLATES)
