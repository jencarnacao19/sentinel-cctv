"""The FFmpeg recorder for one camera.

Design decisions that exist to protect footage:

* **Stream copy by default.** ``-c:v copy`` writes the camera's own H.264/H.265
  bitstream untouched. Recording a 2K camera then costs a few percent of one
  core instead of most of one, which is what makes nine cameras viable on a
  small server. Audio is normalised to AAC because MP4 cannot carry the G.711
  PCM codecs commonly emitted by NVR channels.
* **Short segments.** A power cut can cost at most one segment, and retention
  can delete the oldest footage without rewriting anything.
* **Fragmented MP4.** A conventional MP4 is unplayable without the ``moov`` atom
  written at the end, so an interrupted file is lost entirely. Fragmented output
  writes an index per fragment, so a segment cut off mid-write still plays up to
  the point it stopped.
* **Input from go2rtc.** Consumer cameras commonly cap concurrent RTSP clients
  at two or three; between the recorder, motion detection, the browser and Home
  Assistant that limit is reached immediately. Reading from go2rtc's local RTSP
  means one connection to the camera regardless of how many consumers there are.

Which input a recorder uses is decided by :mod:`app.recording.source_policy`,
and how forgiving its FFmpeg arguments are is decided by
:mod:`app.recording.profiles`. This module owns the input *description* and the
command assembly; it makes no policy decisions of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.cameras.service import resolve_stream_url
from app.config import Settings
from app.database.models import Camera, RecorderSource, RecordingProfile
from app.recording import paths, profiles

#: Re-exported so callers that only build commands need one import. The
#: authoritative definitions live in :mod:`app.recording.profiles`.
FRAGMENTED_MP4_FLAGS = profiles.FRAGMENTED_MP4_FLAGS
INPUT_TIMEOUT_SECONDS = profiles.INPUT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class RecorderInput:
    """Where a recorder reads from, and how to describe it in a log."""

    url: str
    via_go2rtc: bool

    @property
    def description(self) -> str:
        return "go2rtc" if self.via_go2rtc else "camera"

    @property
    def source(self) -> RecorderSource:
        return RecorderSource.RELAY if self.via_go2rtc else RecorderSource.DIRECT


def relay_input(camera: Camera, settings: Settings) -> RecorderInput:
    """The shared local relay's RTSP endpoint for this camera."""
    return RecorderInput(
        url=f"rtsp://{settings.go2rtc_host}:{settings.go2rtc_rtsp_port}/{camera.stream_name}",
        via_go2rtc=True,
    )


def direct_input(camera: Camera) -> RecorderInput | None:
    """The camera's own RTSP endpoint, or ``None`` when it cannot be resolved."""
    direct = resolve_stream_url(camera)
    return RecorderInput(url=direct, via_go2rtc=False) if direct else None


def input_for_source(
    camera: Camera, settings: Settings, source: RecorderSource
) -> RecorderInput | None:
    """Resolve a policy decision into a concrete input, or ``None``."""
    if source is RecorderSource.RELAY:
        return relay_input(camera, settings) if settings.record_via_go2rtc else None
    return direct_input(camera)


def resolve_input(camera: Camera, settings: Settings) -> RecorderInput | None:
    """Prefer go2rtc's local RTSP; fall back to the camera itself."""
    if settings.record_via_go2rtc:
        return relay_input(camera, settings)
    return direct_input(camera)


def build_record_command(
    source: RecorderInput,
    output_pattern: str,
    *,
    ffmpeg_binary: str,
    segment_seconds: int,
    profile: RecordingProfile = RecordingProfile.PASSTHROUGH,
    fps: float | None = None,
) -> list[str]:
    """Build the FFmpeg argument list for continuous segmented recording.

    Returned as a list and never joined into a shell string: the URL may carry
    the camera password, and the output path is derived from a camera id.
    """
    if profile is RecordingProfile.AUTO:
        raise ValueError("AUTO must be resolved to a concrete profile before building a command.")

    return [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        *profiles.input_arguments(profile),
        "-i",
        source.url,
        *profiles.output_arguments(profile, fps=fps),
        *profiles.segment_arguments(segment_seconds=segment_seconds),
        output_pattern,
    ]


def build_recorder_environment() -> dict[str, str]:
    """FFmpeg expands ``strftime`` in local time.

    Forcing UTC keeps filenames aligned with the UTC timestamps in the database,
    and avoids the twice-yearly daylight-saving change that would otherwise
    produce an hour of overwritten or out-of-order filenames.
    """
    return {"TZ": "UTC"}


def prepare_output(root: Path, camera_id: str, *, now: datetime | None = None) -> str:
    """Create the directories FFmpeg needs and return its output pattern."""
    moment = now or datetime.now(UTC)
    paths.ensure_hour_directories(root, camera_id, moment)
    return paths.output_pattern(root, camera_id)


def command_for_camera(
    camera: Camera,
    root: Path,
    settings: Settings,
    segment_seconds: int,
    *,
    now: datetime | None = None,
    profile: RecordingProfile = RecordingProfile.PASSTHROUGH,
) -> Sequence[str]:
    """Full command for one camera, with its output directories created."""
    source = resolve_input(camera, settings)
    if source is None:
        raise ValueError(f"Camera {camera.name} has no usable stream URL.")
    return build_record_command(
        source,
        prepare_output(root, camera.id, now=now),
        ffmpeg_binary=settings.ffmpeg_binary,
        segment_seconds=segment_seconds,
        profile=profile,
        fps=camera.fps,
    )
