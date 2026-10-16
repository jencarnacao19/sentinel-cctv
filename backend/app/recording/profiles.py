"""FFmpeg argument construction for the three recording profiles.

Every flag here exists because a specific, observed failure required it. The
reference installation produced, in four hours on one NVR channel: 25,146 RTSP
``bad cseq`` warnings, 13,102 ``Empty H.264 RTP packet`` warnings and 5,229
``non-existing PPS N referenced`` errors, while another camera logged
``Could not write header for output file #0 (incorrect codec parameters ?)``
seven times. Those are three distinct problems -- protocol desync, corrupt
payloads, and missing H.264 parameter sets -- and they need different answers.

The profiles
------------

``PASSTHROUGH``
    What Sentinel has always done: ``-c:v copy``, the camera's own bitstream
    written untouched. Cheapest and highest fidelity, and correct for a camera
    that behaves. Kept byte-for-byte identical to the previous command so a
    healthy install sees no change at all.

``RESILIENT``
    Still a stream copy -- no decode, no re-encode, so still a few percent of a
    core -- but told to survive a misbehaving source: discard corrupt packets
    instead of muxing them, keep going past bitstream errors, regenerate
    timestamps, and give up probing quickly rather than hanging for thirty
    seconds before failing to write a header.

``NORMALISED``
    Decode and re-encode to H.264 with a predictable keyframe cadence. The only
    profile that can fix a stream whose *bitstream itself* is malformed, and
    the only way to make an H.265 camera playable in a browser. It costs most
    of a core per camera on the reference host, so it is never selected
    automatically.

What no profile does
--------------------

None of them fabricates footage. ``-fps_mode vfr`` is used deliberately in the
normalised profile precisely because the constant-frame-rate modes *duplicate*
frames to fill time -- which would paper over an outage with frozen video and
make a real gap unmeasurable. When the camera sends nothing, nothing is
written, the segment is short, and the gap stays visible in the timeline.
"""

from __future__ import annotations

from app.database.models import RecordingProfile

#: Fragment on every keyframe so an interrupted file remains playable. The flag
#: is ``default_base_moof``, not ``default_base_is_moof`` -- FFmpeg rejects the
#: whole muxer configuration if it is wrong, which means no recording at all
#: rather than a degraded one.
FRAGMENTED_MP4_FLAGS = "+frag_keyframe+empty_moov+default_base_moof"

#: Socket read timeout handed to FFmpeg's RTSP demuxer, in seconds.
INPUT_TIMEOUT_SECONDS = 15

#: Bounded probe for the resilient and normalised profiles, in microseconds.
#:
#: The default probe reads up to five megabytes or five seconds looking for
#: stream parameters. A camera that never repeats its SPS/PPS makes FFmpeg
#: spend that whole budget and *then* fail to open the muxer, so the recorder
#: burns a restart cycle before it learns anything. Bounding it turns a slow
#: failure into a fast one, which the supervisor's backoff handles far better.
PROBE_MICROSECONDS = 3_000_000
PROBE_BYTES = 3_000_000

#: RTP reorder window for the resilient profile. Larger than FFmpeg's default
#: so a burst of out-of-order packets is reassembled rather than reported as
#: corruption; small enough that it cannot become an unbounded memory sink.
REORDER_QUEUE_SIZE = 2048

#: Maximum demuxer reordering delay, in microseconds.
MAX_DELAY_MICROSECONDS = 5_000_000

#: Keyframe cadence for the normalised profile, in seconds. Two seconds keeps
#: seeking responsive and bounds how much footage one corrupt GOP can cost,
#: while staying cheap enough for software encoding.
KEYFRAME_INTERVAL_SECONDS = 2

#: x264 preset for normalisation. ``veryfast`` is the fastest preset that still
#: produces a clean, standards-compliant stream; anything faster trades away
#: more quality than it buys back in CPU on this class of host.
NORMALISE_PRESET = "veryfast"

#: Constant-quality target. Deliberately high: normalisation exists to make
#: footage usable, not to shrink it, and reducing quality is an administrator's
#: explicit choice rather than a side effect of repairing a stream.
NORMALISE_CRF = 20


def input_arguments(profile: RecordingProfile) -> list[str]:
    """Demuxer-side flags, which is where a misbehaving camera is handled."""
    arguments = [
        # Without this FFmpeg treats the inherited stdin as a control terminal
        # and can consume keystrokes meant for the parent process.
        "-nostdin",
        # TCP interleaving: UDP loses packets on a busy LAN, and a lost packet
        # in an H.264 reference frame corrupts every frame until the next
        # keyframe.
        "-rtsp_transport",
        "tcp",
        "-timeout",
        str(INPUT_TIMEOUT_SECONDS * 1_000_000),
    ]

    if profile is RecordingProfile.PASSTHROUGH:
        return arguments

    arguments += [
        # Bounded probe: fail fast instead of hanging before a header error.
        "-analyzeduration",
        str(PROBE_MICROSECONDS),
        "-probesize",
        str(PROBE_BYTES),
        # discardcorrupt drops packets FFmpeg knows are damaged rather than
        # muxing them, which is the difference between a shorter honest segment
        # and a segment full of visual corruption. genpts synthesises the
        # presentation timestamps that an "Empty H.264 RTP packet" storm leaves
        # missing.
        "-fflags",
        "+genpts+discardcorrupt",
        # Keep going through bitstream errors instead of aborting the process.
        # The alternative is a restart cycle per corrupt GOP.
        "-err_detect",
        "ignore_err",
        # Reassemble out-of-order RTP rather than reporting it as loss.
        "-reorder_queue_size",
        str(REORDER_QUEUE_SIZE),
        "-max_delay",
        str(MAX_DELAY_MICROSECONDS),
    ]
    return arguments


def output_arguments(profile: RecordingProfile, *, fps: float | None) -> list[str]:
    """Encoder/muxer-side flags."""
    arguments = [
        # First video track, and audio only if the camera has any. Without the
        # explicit map, a camera advertising two video tracks records the wrong
        # one; without the '?', a camera with no audio fails to start at all.
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
    ]

    if profile is RecordingProfile.NORMALISED:
        gop = max(round((fps or 15.0) * KEYFRAME_INTERVAL_SECONDS), 1)
        arguments += [
            "-c:v",
            "libx264",
            "-preset",
            NORMALISE_PRESET,
            "-crf",
            str(NORMALISE_CRF),
            # 4:2:0 8-bit is what every browser decoder accepts; a camera
            # sending 4:2:2 or 10-bit would otherwise produce files that only
            # play in a desktop media player.
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            # A fixed GOP with forced keyframes gives the timeline predictable
            # seek points and bounds how much a single damaged GOP can cost.
            "-g",
            str(gop),
            "-keyint_min",
            str(gop),
            # Closed GOPs only: an open GOP references frames across a segment
            # boundary, which is exactly what makes a segmented recording
            # unplayable from its own start.
            "-sc_threshold",
            "0",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{KEYFRAME_INTERVAL_SECONDS})",
            # vfr, never cfr. The constant-frame-rate modes duplicate frames to
            # fill missing time, which would turn a real outage into frozen
            # video and hide the gap this system exists to make visible.
            "-fps_mode",
            "vfr",
        ]
    else:
        # Stream copy writes the camera's own bitstream untouched, which is why
        # recording a 2K camera costs a few percent of one core rather than
        # most of one.
        arguments += ["-c:v", "copy"]

    arguments += [
        # MP4 cannot mux the pcm_mulaw/pcm_alaw audio many cameras expose. AAC
        # encoding is inexpensive next to video and preserves sound rather than
        # making those cameras fail to record altogether.
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        # Some cameras emit negative timestamps after a reconnect, which makes
        # the muxer drop the first frames of the segment.
        "-avoid_negative_ts",
        "make_zero",
    ]
    return arguments


def segment_arguments(*, segment_seconds: int) -> list[str]:
    """Muxer flags that turn one long output into clock-aligned segments."""
    return [
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        # Cut on clock boundaries so a segment's filename and its content agree,
        # and so the timeline has no drifting offsets to correct for.
        "-segment_atclocktime",
        "1",
        "-reset_timestamps",
        "1",
        "-segment_format",
        "mp4",
        "-segment_format_options",
        f"movflags={FRAGMENTED_MP4_FLAGS}",
        "-strftime",
        "1",
    ]


def resolve_profile(
    configured: RecordingProfile,
    *,
    corruption_observed: bool,
) -> RecordingProfile:
    """Turn ``AUTO`` into the profile that should actually run.

    Escalation is one-way and only as far as ``RESILIENT``. Normalisation is
    never reached automatically: it is the only profile that costs real CPU,
    and on a host already running eight recorders and eight motion workers that
    is a decision an administrator makes with the measurements in front of
    them, not one a background heuristic makes on their behalf.
    """
    if configured is not RecordingProfile.AUTO:
        return configured
    return RecordingProfile.RESILIENT if corruption_observed else RecordingProfile.PASSTHROUGH
