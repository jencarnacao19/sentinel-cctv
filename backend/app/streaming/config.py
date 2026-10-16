"""Generation of go2rtc's configuration file.

The user never writes or sees this file. It is derived entirely from the camera
list and rewritten whenever that list changes.

It contains camera passwords in plaintext -- go2rtc has no other way to
authenticate to an RTSP source -- so it is written with 0600 permissions inside
the data directory, is excluded from backups and diagnostics, and is never
served over HTTP.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from app.cameras.service import resolve_stream_url
from app.config import Settings
from app.database.models import Camera
from app.logging_config import get_logger

logger = get_logger("GO2RTC")

SUB_STREAM_SUFFIX = "_sub"

#: Appended to every source URL.
#:
#: ``#backchannel=0`` disables two-way audio negotiation. Many consumer cameras,
#: Tapo included, advertise a backchannel and then mishandle the SETUP request,
#: which shows up as a stream that connects and immediately drops. Sentinel does
#: not offer talk-back, so there is nothing to lose by refusing it.
SOURCE_SUFFIX = "#backchannel=0"

# G.711 is what Tapo cameras and many NVR channels expose. It is valid RTSP
# audio and FFmpeg can record it, but MSE/fMP4 browsers require AAC. Keep the
# original source intact, then offer one lazy, local AAC producer through the
# existing relay. This does not open a second connection to the camera.
G711_AUDIO_CODECS = frozenset({"pcm_alaw", "pcm_mulaw", "pcma", "pcmu", "g711a", "g711u"})


def stream_names(camera: Camera) -> tuple[str, str | None]:
    """Return ``(main_stream_name, sub_stream_name_or_None)``."""
    main = camera.stream_name
    sub = f"{main}{SUB_STREAM_SUFFIX}" if camera.sub_stream_url else None
    return main, sub


def build_streams(
    cameras: Iterable[Camera],
    *,
    go2rtc_host: str = "127.0.0.1",
    go2rtc_rtsp_port: int = 8554,
) -> dict[str, list[str]]:
    """Map go2rtc stream names to their source URLs.

    Disabled cameras are omitted entirely rather than defined and left unused:
    go2rtc connects lazily, but an absent stream makes "this camera is off" an
    unambiguous state instead of a silent one.
    """
    streams: dict[str, list[str]] = {}
    for camera in cameras:
        if not camera.enabled:
            continue

        main_url = resolve_stream_url(camera)
        if not main_url:
            logger.warning(
                "Camera has no usable main stream URL; skipping",
                extra={"camera": camera.name, "camera_id": camera.id},
            )
            continue

        main_name, sub_name = stream_names(camera)
        streams[main_name] = [f"{main_url}{SOURCE_SUFFIX}"]
        if _needs_browser_audio(camera):
            streams[main_name].append(_aac_relay_source(main_name, go2rtc_host, go2rtc_rtsp_port))

        if sub_name:
            sub_url = resolve_stream_url(camera, sub=True)
            if sub_url:
                streams[sub_name] = [f"{sub_url}{SOURCE_SUFFIX}"]
                if _needs_browser_audio(camera):
                    streams[sub_name].append(
                        _aac_relay_source(sub_name, go2rtc_host, go2rtc_rtsp_port)
                    )
    return streams


def _needs_browser_audio(camera: Camera) -> bool:
    codec = (camera.audio_codec or "").strip().lower()
    return codec in G711_AUDIO_CODECS


def _aac_relay_source(name: str, host: str, port: int) -> str:
    """Return a credential-free, lazy AAC view over the primary relay source."""
    return f"ffmpeg:rtsp://{host}:{port}/{name}?video&audio#audio=aac"


def build_config(settings: Settings, cameras: Iterable[Camera]) -> dict[str, Any]:
    """Build the whole go2rtc configuration document."""
    return {
        # Loopback only. go2rtc's built-in admin interface would otherwise be
        # reachable on the LAN with no authentication at all; the browser talks
        # to it through Sentinel's authenticated proxy instead.
        "api": {
            "listen": f"{settings.go2rtc_host}:{settings.go2rtc_api_port}",
            "origin": "*",
        },
        "rtsp": {
            "listen": f"{settings.go2rtc_host}:{settings.go2rtc_rtsp_port}",
            # TCP interleaved: UDP loses packets on a busy home network and shows
            # up as tearing in the recording.
            # Do not apply the mp4 codec filter here. Many cameras expose G.711,
            # which the recorder accepts and converts to AAC. Filtering at the
            # relay used to discard audio before either consumer could see it.
            "default_query": "video&audio",
        },
        # WebRTC needs a port reachable from the browser, so this one does bind
        # on all interfaces. It carries media only, never control.
        "webrtc": {
            "listen": f":{settings.go2rtc_webrtc_port}",
            "ice_servers": [],
        },
        "ffmpeg": {"bin": settings.ffmpeg_binary},
        "log": {"level": "warn"},
        "streams": build_streams(
            cameras,
            go2rtc_host=settings.go2rtc_host,
            go2rtc_rtsp_port=settings.go2rtc_rtsp_port,
        ),
    }


def render_config(settings: Settings, cameras: Iterable[Camera]) -> str:
    return yaml.safe_dump(
        build_config(settings, cameras), sort_keys=False, default_flow_style=False, width=4096
    )


def write_config(settings: Settings, cameras: Iterable[Camera]) -> tuple[Path, bool]:
    """Write go2rtc.yaml. Returns ``(path, changed)``.

    ``changed`` is what decides whether go2rtc needs restarting, so an unrelated
    settings save does not interrupt every live stream in the house.
    """
    path = settings.go2rtc_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_config(settings, cameras)

    previous = path.read_text(encoding="utf-8") if path.exists() else None
    if previous == rendered:
        return path, False

    # Written via a temporary file in the same directory so a crash mid-write
    # cannot leave go2rtc with a truncated config.
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    _restrict_permissions(temporary)
    os.replace(temporary, path)
    _restrict_permissions(path)

    logger.info(
        "go2rtc configuration written",
        extra={"path": str(path), "streams": rendered.count("\n  cam_")},
    )
    return path, True


def _restrict_permissions(path: Path) -> None:
    """0600 where the platform supports it. The file holds camera passwords."""
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError):  # pragma: no cover - non-POSIX
        logger.debug("Could not restrict permissions", extra={"path": str(path)})
