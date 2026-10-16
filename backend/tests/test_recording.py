"""Recording: path layout, FFmpeg arguments, indexing and the recordings API."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import Settings
from app.database.models import (
    Camera,
    RecorderSource,
    RecordingProfile,
    RecordingSegment,
    RecordingSourceMode,
    SegmentStatus,
)
from app.database.session import session_scope
from app.recording import corruption, indexer, paths, recorder
from app.recording.manager import RecordingManager
from app.recording.profiles import PROBE_BYTES, PROBE_MICROSECONDS, resolve_profile
from app.recording.recorder import RecorderInput, build_record_command
from app.recording.source_policy import (
    BASE_BLOCK_SECONDS,
    MAX_BLOCK_SECONDS,
    QUALIFY_SECONDS,
    SWITCH_COOLDOWN_SECONDS,
    SourceOutcome,
    SourcePolicy,
    SourceReason,
    block_seconds,
    decode_states,
    encode_states,
    prune_states,
)
from app.storage.root import (
    MARKER_FILENAME,
    check_storage,
    prepare_storage,
)

FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for this test")

CAMERA_PAYLOAD = {
    "name": "Entrada",
    "main_stream_url": "rtsp://192.168.1.50:554/stream1",
    "username": "admin",
    "password": "hunter2",
}

CAMERA_ID = "11111111-2222-3333-4444-555555555555"

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


class TestPathLayout:
    def test_the_pattern_nests_by_hour(self) -> None:
        pattern = paths.output_pattern(Path("/recordings"), CAMERA_ID)
        assert pattern.replace("\\", "/").endswith(f"{CAMERA_ID}/%Y/%m/%d/%H/segment-%H-%M-%S.mp4")

    def test_a_start_time_round_trips_through_the_path(self) -> None:
        relative = f"{CAMERA_ID}/2026/08/13/15/segment-15-32-00.mp4"
        assert paths.parse_segment_start(relative) == datetime(2026, 8, 13, 15, 32, 0, tzinfo=UTC)

    def test_ffmpegs_collision_suffix_is_understood(self) -> None:
        """FFmpeg appends _1 when a filename already exists, which happens after
        a restart inside the same second."""
        relative = f"{CAMERA_ID}/2026/08/13/15/segment-15-32-00_1.mp4"
        assert paths.parse_segment_start(relative) is not None

    @pytest.mark.parametrize(
        "relative",
        [
            "notes.txt",
            f"{CAMERA_ID}/random.mp4",
            f"{CAMERA_ID}/2026/08/13/15/segment-25-99-99.mp4",
            "2026/08/13/15/segment-15-32-00.mp4x",
        ],
    )
    def test_unrecognised_names_are_not_given_a_timestamp(self, relative: str) -> None:
        assert paths.parse_segment_start(relative) is None

    def test_hour_directories_are_created_ahead_of_the_clock(self, tmp_path: Path) -> None:
        """FFmpeg's segment muxer expands strftime but will not create
        directories, so the next hour has to exist before the clock reaches it."""
        moment = datetime(2026, 8, 13, 23, 45, tzinfo=UTC)
        created = paths.ensure_hour_directories(tmp_path, CAMERA_ID, moment, hours_ahead=2)

        assert len(created) == 3
        assert all(directory.is_dir() for directory in created)
        # Crossing midnight must roll the date, not just the hour.
        assert (tmp_path / CAMERA_ID / "2026" / "08" / "14" / "00").is_dir()
        assert (tmp_path / CAMERA_ID / "2026" / "08" / "14" / "01").is_dir()

    def test_paths_are_stored_relative_to_the_root(self, tmp_path: Path) -> None:
        target = tmp_path / CAMERA_ID / "2026" / "08" / "13" / "15" / "segment-15-00-00.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x")
        assert paths.relative_path(tmp_path, target).startswith(CAMERA_ID)

    def test_camera_id_is_recovered_from_a_relative_path(self) -> None:
        relative = f"{CAMERA_ID}/2026/08/13/15/segment-15-00-00.mp4"
        assert paths.camera_id_from_relative(relative) == CAMERA_ID


class TestRecorderCommand:
    def _command(self, **overrides: object) -> list[str]:
        defaults: dict[str, object] = {
            "ffmpeg_binary": "ffmpeg",
            "segment_seconds": 60,
        }
        defaults.update(overrides)
        return build_record_command(
            RecorderInput(url="rtsp://127.0.0.1:8554/cam_x", via_go2rtc=True),
            "/recordings/cam/%Y/%m/%d/%H/segment-%H-%M-%S.mp4",
            **defaults,  # type: ignore[arg-type]
        )

    def test_it_copies_rather_than_re_encodes(self) -> None:
        """Stream copy is what makes nine cameras cost a few percent of a core
        instead of most of one."""
        command = self._command()
        assert command[command.index("-c:v") + 1] == "copy"
        assert "libx264" not in command

    def test_it_normalises_camera_audio_for_mp4(self) -> None:
        """G.711/PCM is common on NVRs but cannot be copied into MP4."""
        command = self._command()
        assert command[command.index("-c:a") + 1] == "aac"
        assert command[command.index("-b:a") + 1] == "64k"

    def test_output_is_fragmented_mp4(self) -> None:
        """A conventional MP4 has no moov atom until it is closed, so a segment
        interrupted by a power cut is unplayable. Fragmented output survives."""
        command = self._command()
        options = command[command.index("-segment_format_options") + 1]
        assert "frag_keyframe" in options
        assert "empty_moov" in options
        # FFmpeg's flag is default_base_moof; the plausible-looking
        # default_base_is_moof makes it reject the whole muxer configuration,
        # which means no recording at all.
        assert "default_base_moof" in options
        assert "default_base_is_moof" not in options

    def test_segments_are_cut_on_clock_boundaries(self) -> None:
        command = self._command(segment_seconds=60)
        assert command[command.index("-segment_time") + 1] == "60"
        assert command[command.index("-segment_atclocktime") + 1] == "1"
        assert command[command.index("-strftime") + 1] == "1"

    def test_audio_is_optional_and_video_is_pinned(self) -> None:
        """Without the explicit map a camera advertising two video tracks records
        the wrong one; without the '?' a camera with no audio fails to start."""
        command = self._command()
        maps = [command[i + 1] for i, arg in enumerate(command) if arg == "-map"]
        assert maps == ["0:v:0", "0:a?"]

    def test_rtsp_uses_tcp_with_a_timeout(self) -> None:
        command = self._command()
        assert command[command.index("-rtsp_transport") + 1] == "tcp"
        assert int(command[command.index("-timeout") + 1]) > 0

    def test_the_url_is_a_single_argument(self) -> None:
        command = self._command()
        assert command[command.index("-i") + 1] == "rtsp://127.0.0.1:8554/cam_x"

    def test_the_environment_forces_utc(self) -> None:
        """FFmpeg expands strftime in local time; without this the filenames
        would drift with the host timezone and repeat an hour every autumn."""
        assert recorder.build_recorder_environment()["TZ"] == "UTC"


class TestRecorderInput:
    def test_it_prefers_go2rtc(self, settings: Settings) -> None:
        """Consumer cameras cap concurrent RTSP clients at two or three, which
        the recorder, motion, the browser and Home Assistant reach at once."""
        camera = Camera(
            id=CAMERA_ID,
            name="Entrada",
            host="192.168.1.50",
            main_stream_url="rtsp://192.168.1.50:554/stream1",
        )
        chosen = recorder.resolve_input(camera, settings)
        assert chosen is not None
        assert chosen.via_go2rtc is True
        assert chosen.source is RecorderSource.RELAY
        assert chosen.url.endswith(camera.stream_name)

    def test_a_direct_input_is_used_when_the_relay_is_switched_off(
        self, settings: Settings
    ) -> None:
        camera = Camera(
            id=CAMERA_ID,
            name="Entrada",
            host="192.168.1.50",
            main_stream_url="rtsp://192.168.1.50:554/stream1",
        )
        direct_settings = settings.model_copy(update={"record_via_go2rtc": False})
        chosen = recorder.resolve_input(camera, direct_settings)
        assert chosen is not None
        assert chosen.via_go2rtc is False
        assert chosen.source is RecorderSource.DIRECT


class TestSourcePolicy:
    """The hysteresis that replaced blind alternation.

    The production failure this exists to prevent: the previous selector
    flipped source on *every* abnormal exit, which produced sixteen switches on
    one camera in four hours and drifted six of eight cameras onto a direct
    connection -- a second RTSP session per camera, on hardware that caps
    sessions at two or three.
    """

    @staticmethod
    def _policy(**kwargs: object) -> SourcePolicy:
        options: dict[str, object] = {"relay_available": True, "direct_available": True}
        options.update(kwargs)
        return SourcePolicy(**options)  # type: ignore[arg-type]

    @staticmethod
    def _fail(policy: SourcePolicy, now: datetime, *, uptime: float = 1.0) -> None:
        policy.record_outcome(
            SourceOutcome(
                source=policy.state.active,
                uptime_seconds=uptime,
                return_code=1,
                counts_as_failure=True,
            ),
            now,
        )

    def test_the_relay_is_home(self) -> None:
        policy = self._policy()
        assert policy.decision().source is RecorderSource.RELAY
        assert policy.decision().reason is SourceReason.RELAY_PREFERRED

    def test_one_failure_does_not_move_a_camera(self) -> None:
        """A camera reboot, a go2rtc restart and a brief LAN drop all produce
        exactly one failure, and all of them heal themselves."""
        policy = self._policy()
        self._fail(policy, NOW)
        assert policy.decision().source is RecorderSource.RELAY
        assert policy.state.consecutive_failures == 1

    def test_two_failures_still_do_not_move_a_camera(self) -> None:
        policy = self._policy()
        self._fail(policy, NOW)
        self._fail(policy, NOW + timedelta(seconds=5))
        assert policy.decision().source is RecorderSource.RELAY

    def test_the_threshold_of_failures_switches_to_direct(self) -> None:
        policy = self._policy()
        for offset in (0, 5, 15):
            self._fail(policy, NOW + timedelta(seconds=offset))
        decision = policy.decision()
        assert decision.source is RecorderSource.DIRECT
        assert decision.reason is SourceReason.RELAY_UNAVAILABLE

    def test_a_switch_is_not_followed_by_another_inside_the_cooldown(self) -> None:
        """The anti-flap guarantee. Direct failing immediately must not bounce
        straight back, or the two sources ping-pong at the restart rate."""
        policy = self._policy()
        for offset in (0, 5, 15):
            self._fail(policy, NOW + timedelta(seconds=offset))
        assert policy.decision().source is RecorderSource.DIRECT

        for offset in (20, 25, 30, 35, 40):
            self._fail(policy, NOW + timedelta(seconds=offset))
        assert policy.decision().source is RecorderSource.DIRECT
        assert policy.state.switch_count == 1

    def test_four_hours_of_total_failure_produce_a_handful_of_switches(self) -> None:
        """The regression test for the observed production behaviour.

        Both sources failing continuously for four hours previously produced a
        switch on every exit. The cooldown bounds it to the number of cooldown
        windows in the period.
        """
        policy = self._policy()
        moment = NOW
        for _ in range(4 * 60 * 2):  # a failure every thirty seconds
            self._fail(policy, moment)
            moment += timedelta(seconds=30)
        expected_ceiling = (4 * 3600) / SWITCH_COOLDOWN_SECONDS + 1
        assert policy.state.switch_count <= expected_ceiling
        assert policy.state.switch_count <= 16

    def test_a_source_that_stays_up_is_qualified_and_forgiven(self) -> None:
        policy = self._policy()
        self._fail(policy, NOW)
        self._fail(policy, NOW + timedelta(seconds=5))
        policy.record_outcome(
            SourceOutcome(
                source=RecorderSource.RELAY,
                uptime_seconds=QUALIFY_SECONDS + 1,
                return_code=1,
                counts_as_failure=True,
            ),
            NOW + timedelta(seconds=400),
        )
        assert policy.state.qualified is True
        # A long clean run then dying is the first failure of a new incident,
        # not the third of an old ladder.
        assert policy.state.consecutive_failures == 1
        assert policy.decision().source is RecorderSource.RELAY

    def test_one_complete_segment_qualifies_a_session_that_then_reaches_eof(self) -> None:
        """Consumer RTSP servers may close between segments without losing footage.

        A session that completed the default sixty-second segment must not be
        treated like a connection that only opened briefly, or its source is
        exponentially blocked despite continuously producing playable files.
        """
        policy = self._policy()
        policy.record_outcome(
            SourceOutcome(
                source=RecorderSource.RELAY,
                uptime_seconds=60.1,
                return_code=0,
                counts_as_failure=True,
            ),
            NOW + timedelta(seconds=61),
        )
        assert policy.state.qualified is True
        assert policy.state.consecutive_failures == 1
        assert policy.state.blocked_until == {}

    def test_a_deliberate_stop_is_never_counted_against_a_source(self) -> None:
        policy = self._policy()
        for offset in (0, 5, 15):
            policy.record_outcome(
                SourceOutcome(
                    source=RecorderSource.RELAY,
                    uptime_seconds=2.0,
                    return_code=-15,
                    counts_as_failure=False,
                ),
                NOW + timedelta(seconds=offset),
            )
        assert policy.decision().source is RecorderSource.RELAY
        assert policy.state.consecutive_failures == 0

    def test_a_failed_source_is_blocked_for_an_exponential_cooldown(self) -> None:
        policy = self._policy()
        for offset in (0, 5, 15):
            self._fail(policy, NOW + timedelta(seconds=offset))
        assert policy.blocked_for_seconds(RecorderSource.RELAY, NOW + timedelta(seconds=15)) > 0
        assert block_seconds(1) == BASE_BLOCK_SECONDS
        assert block_seconds(2) == BASE_BLOCK_SECONDS * 2
        assert block_seconds(99) == MAX_BLOCK_SECONDS

    def test_a_camera_returns_home_once_the_relay_cooldown_expires(self) -> None:
        """One bad hour must not pin a camera to the expensive path for ever."""
        policy = self._policy()
        for offset in (0, 5, 15):
            self._fail(policy, NOW + timedelta(seconds=offset))
        assert policy.decision().source is RecorderSource.DIRECT

        later = NOW + timedelta(seconds=BASE_BLOCK_SECONDS + SWITCH_COOLDOWN_SECONDS + 10)
        decision = policy.record_start(later)
        assert decision.source is RecorderSource.RELAY
        assert decision.reason is SourceReason.RELAY_REQUALIFYING

    def test_a_failed_relay_requalification_rolls_back_without_another_cooldown(
        self,
    ) -> None:
        """A cautious home trial must not strand recording on the bad relay."""
        policy = self._policy()
        for offset in (0, 5, 15):
            self._fail(policy, NOW + timedelta(seconds=offset))
        assert policy.decision().source is RecorderSource.DIRECT

        # Prove the fallback, then wait until the penalised relay is due for a
        # cautious requalification.
        policy.record_outcome(
            SourceOutcome(
                source=RecorderSource.DIRECT,
                uptime_seconds=QUALIFY_SECONDS + 1,
                return_code=0,
                counts_as_failure=True,
            ),
            NOW + timedelta(seconds=100),
        )
        trial = NOW + timedelta(seconds=BASE_BLOCK_SECONDS + SWITCH_COOLDOWN_SECONDS + 10)
        assert policy.record_start(trial).source is RecorderSource.RELAY
        assert policy.decision().reason is SourceReason.RELAY_REQUALIFYING

        for offset in (1, 6, 16):
            self._fail(policy, trial + timedelta(seconds=offset))
        assert policy.decision().source is RecorderSource.DIRECT
        assert policy.decision().reason is SourceReason.RELAY_UNAVAILABLE
        assert policy.state.switch_count == 3

    def test_the_relay_stays_home_while_it_is_still_blocked(self) -> None:
        policy = self._policy()
        for offset in (0, 5, 15):
            self._fail(policy, NOW + timedelta(seconds=offset))
        soon = NOW + timedelta(seconds=SWITCH_COOLDOWN_SECONDS + 10)
        assert policy.record_start(soon).source is RecorderSource.DIRECT

    def test_no_alternate_means_the_camera_keeps_retrying_where_it_is(self) -> None:
        """A camera with no direct URL must not stop recording; the supervisor
        backoff already paces the attempts."""
        policy = self._policy(direct_available=False)
        for offset in (0, 5, 15, 25, 35):
            self._fail(policy, NOW + timedelta(seconds=offset))
        assert policy.decision().source is RecorderSource.RELAY
        assert policy.state.switch_count == 0

    def test_a_relay_pin_never_fails_over_to_the_defective_direct_path(self) -> None:
        policy = self._policy(pinned_source=RecorderSource.RELAY)
        for offset in range(0, 600, 15):
            self._fail(policy, NOW + timedelta(seconds=offset))
        assert policy.decision().source is RecorderSource.RELAY
        assert policy.decision().reason is SourceReason.RELAY_PINNED
        assert policy.state.switch_count == 0

    def test_a_direct_pin_overrides_old_relay_state(self) -> None:
        policy = self._policy(pinned_source=RecorderSource.DIRECT)
        assert policy.decision().source is RecorderSource.DIRECT
        assert policy.decision().reason is SourceReason.DIRECT_PINNED

    def test_a_relayless_install_stays_direct(self) -> None:
        policy = self._policy(relay_available=False)
        assert policy.decision().source is RecorderSource.DIRECT
        assert policy.decision().reason is SourceReason.DIRECT_ONLY

    def test_reset_clears_every_penalty_and_returns_home(self) -> None:
        policy = self._policy()
        shared_state = policy.state
        for offset in (0, 5, 15):
            self._fail(policy, NOW + timedelta(seconds=offset))
        assert policy.decision().source is RecorderSource.DIRECT

        decision = policy.reset(NOW + timedelta(seconds=20))
        assert decision.source is RecorderSource.RELAY
        assert decision.reason is SourceReason.OPERATOR_RESET
        assert policy.state is shared_state
        assert policy.state.blocked_until == {}
        assert policy.state.failed_qualifications == {}

    def test_a_naive_persisted_timestamp_is_normalised_to_utc(self) -> None:
        restored = decode_states(
            {CAMERA_ID: {"last_switch_at": "2026-08-27T11:50:00", "active": "direct"}}
        )[CAMERA_ID]
        assert restored.last_switch_at is not None
        assert restored.last_switch_at.tzinfo is UTC
        # The aware manager clock can now be compared without raising.
        SourcePolicy(relay_available=True, direct_available=True, state=restored).record_start(NOW)

    def test_an_outcome_from_an_abandoned_source_is_ignored(self) -> None:
        """A run that finished after the policy already moved on says nothing
        about the current choice."""
        policy = self._policy()
        for offset in (0, 5, 15):
            self._fail(policy, NOW + timedelta(seconds=offset))
        before = policy.state.consecutive_failures
        policy.record_outcome(
            SourceOutcome(
                source=RecorderSource.RELAY,
                uptime_seconds=1.0,
                return_code=1,
                counts_as_failure=True,
            ),
            NOW + timedelta(seconds=16),
        )
        assert policy.state.consecutive_failures == before

    def test_state_survives_a_persistence_round_trip(self) -> None:
        policy = self._policy()
        for offset in (0, 5, 15):
            self._fail(policy, NOW + timedelta(seconds=offset))
        encoded = encode_states({CAMERA_ID: policy.state})
        restored = decode_states(json.loads(json.dumps(encoded)))[CAMERA_ID]
        assert restored.active is policy.state.active
        assert restored.reason is policy.state.reason
        assert restored.switch_count == policy.state.switch_count
        assert restored.blocked_until.keys() == policy.state.blocked_until.keys()

    def test_corrupt_persisted_state_falls_back_to_a_clean_state(self) -> None:
        """A hand-edited or half-written row must never stop a camera recording."""
        restored = decode_states({CAMERA_ID: {"active": "nonsense", "switch_count": "many"}})
        state = restored[CAMERA_ID]
        assert state.active is RecorderSource.RELAY
        assert state.switch_count == 0

    def test_states_for_removed_cameras_are_pruned(self) -> None:
        states = decode_states({CAMERA_ID: {}, "gone": {}})
        assert set(prune_states(states, {CAMERA_ID})) == {CAMERA_ID}


class TestRecordingProfiles:
    """Every flag has to be justified, so every flag is asserted."""

    @staticmethod
    def _command(profile: RecordingProfile, *, fps: float | None = 15.0) -> list[str]:
        return build_record_command(
            RecorderInput(url="rtsp://relay/cam", via_go2rtc=True),
            "/recordings/cam/%Y/%m/%d/%H/segment-%H-%M-%S.mp4",
            ffmpeg_binary="ffmpeg",
            segment_seconds=60,
            profile=profile,
            fps=fps,
        )

    def test_passthrough_is_unchanged_from_the_previous_command(self) -> None:
        """A healthy install must see no change at all from this update."""
        command = self._command(RecordingProfile.PASSTHROUGH)
        assert "-c:v" in command
        assert command[command.index("-c:v") + 1] == "copy"
        # None of the resilience flags leak into the cheap path.
        for flag in ("-fflags", "-err_detect", "-reorder_queue_size", "-analyzeduration"):
            assert flag not in command

    def test_resilient_discards_corrupt_packets_and_repairs_timestamps(self) -> None:
        command = self._command(RecordingProfile.RESILIENT)
        assert command[command.index("-fflags") + 1] == "+genpts+discardcorrupt"
        assert command[command.index("-err_detect") + 1] == "ignore_err"
        # Still a stream copy: repair must not silently cost a core.
        assert command[command.index("-c:v") + 1] == "copy"

    def test_resilient_bounds_the_probe_so_a_bad_source_fails_fast(self) -> None:
        command = self._command(RecordingProfile.RESILIENT)
        assert command[command.index("-analyzeduration") + 1] == str(PROBE_MICROSECONDS)
        assert command[command.index("-probesize") + 1] == str(PROBE_BYTES)

    def test_normalised_re_encodes_with_a_fixed_keyframe_cadence(self) -> None:
        command = self._command(RecordingProfile.NORMALISED, fps=15.0)
        assert command[command.index("-c:v") + 1] == "libx264"
        assert command[command.index("-g") + 1] == "30"
        assert command[command.index("-keyint_min") + 1] == "30"
        assert command[command.index("-pix_fmt") + 1] == "yuv420p"

    def test_normalised_never_duplicates_frames_to_fill_a_gap(self) -> None:
        """The whole point: a real outage must stay a measurable gap, not
        become frozen video."""
        command = self._command(RecordingProfile.NORMALISED)
        assert command[command.index("-fps_mode") + 1] == "vfr"
        assert "cfr" not in command

    def test_a_missing_frame_rate_still_produces_a_valid_gop(self) -> None:
        command = self._command(RecordingProfile.NORMALISED, fps=None)
        assert int(command[command.index("-g") + 1]) >= 1

    def test_every_profile_keeps_tcp_and_the_socket_timeout(self) -> None:
        for profile in (
            RecordingProfile.PASSTHROUGH,
            RecordingProfile.RESILIENT,
            RecordingProfile.NORMALISED,
        ):
            command = self._command(profile)
            assert command[command.index("-rtsp_transport") + 1] == "tcp"
            assert "-timeout" in command

    def test_no_profile_builds_a_shell_string(self) -> None:
        for profile in (
            RecordingProfile.PASSTHROUGH,
            RecordingProfile.RESILIENT,
            RecordingProfile.NORMALISED,
        ):
            command = self._command(profile)
            assert all(isinstance(part, str) for part in command)
            assert not any(" " in part and part.startswith("-") for part in command)

    def test_auto_must_be_resolved_before_a_command_is_built(self) -> None:
        with pytest.raises(ValueError, match="AUTO"):
            self._command(RecordingProfile.AUTO)

    def test_auto_stays_cheap_until_corruption_is_actually_measured(self) -> None:
        assert (
            resolve_profile(RecordingProfile.AUTO, corruption_observed=False)
            is RecordingProfile.PASSTHROUGH
        )
        assert (
            resolve_profile(RecordingProfile.AUTO, corruption_observed=True)
            is RecordingProfile.RESILIENT
        )

    def test_auto_never_escalates_to_normalisation(self) -> None:
        """Re-encoding costs most of a core per camera; that is an
        administrator's decision, not a heuristic's."""
        assert (
            resolve_profile(RecordingProfile.AUTO, corruption_observed=True)
            is not RecordingProfile.NORMALISED
        )

    def test_an_explicit_profile_is_never_overridden(self) -> None:
        for profile in (
            RecordingProfile.PASSTHROUGH,
            RecordingProfile.RESILIENT,
            RecordingProfile.NORMALISED,
        ):
            assert resolve_profile(profile, corruption_observed=True) is profile


class TestCorruptionAccounting:
    """Counted, bounded and never retained as text."""

    @staticmethod
    def _registry() -> corruption.CorruptionRegistry:
        return corruption.CorruptionRegistry()

    def test_the_real_production_signatures_are_classified(self) -> None:
        cases = {
            "[rtsp @ 0x1] Empty H.264 RTP packet": corruption.CorruptionKind.EMPTY_RTP_PACKET,
            "[h264 @ 0x2] non-existing PPS 0 referenced": (
                corruption.CorruptionKind.MISSING_PARAMETER_SET
            ),
            "[h264 @ 0x3] decode_slice_header error": corruption.CorruptionKind.DECODE_ERROR,
            "[rtsp @ 0x4] bad cseq 12 expected=13": corruption.CorruptionKind.RTSP_DESYNC,
            "Could not write header for output file #0": corruption.CorruptionKind.MUXER,
            "rtsp://host/s: Connection timed out": corruption.CorruptionKind.CONNECTION,
        }
        for line, expected in cases.items():
            assert corruption.classify(line) is expected

    def test_classification_never_returns_any_part_of_the_input(self) -> None:
        line = "rtsp://admin:hunter2@192.168.1.50:554/s: Connection refused"
        kind = corruption.classify(line)
        assert kind is corruption.CorruptionKind.CONNECTION
        assert "hunter2" not in kind.value
        assert "192.168.1.50" not in kind.value

    def test_a_repeat_notice_is_credited_to_the_dominant_category(self) -> None:
        registry = self._registry()
        registry.observe(CAMERA_ID, "[rtsp @ 0x1] Empty H.264 RTP packet", now=NOW)
        registry.observe(CAMERA_ID, "Last message repeated 5350 times", now=NOW)
        snapshot = registry.snapshot(CAMERA_ID, now=NOW)
        assert snapshot.total == 5351
        assert snapshot.dominant is corruption.CorruptionKind.EMPTY_RTP_PACKET
        assert snapshot.is_material is True

    def test_the_event_ring_stays_bounded_under_a_storm(self) -> None:
        registry = self._registry()
        for index in range(corruption.MAX_TRACKED_EVENTS * 3):
            registry.observe(
                CAMERA_ID,
                "[rtsp @ 0x1] Empty H.264 RTP packet",
                now=NOW + timedelta(milliseconds=index),
            )
        snapshot = registry.snapshot(CAMERA_ID, now=NOW + timedelta(seconds=1))
        assert snapshot.total == corruption.MAX_TRACKED_EVENTS * 3
        assert snapshot.events_per_minute > 0

    def test_the_production_rate_counts_as_material_and_silence_does_not(self) -> None:
        registry = self._registry()
        snapshot = registry.snapshot(CAMERA_ID, now=NOW)
        assert snapshot.is_material is False

        for index in range(600):
            registry.observe(
                CAMERA_ID,
                "[rtsp @ 0x1] Empty H.264 RTP packet",
                now=NOW + timedelta(seconds=index * 0.5),
            )
        busy = registry.snapshot(CAMERA_ID, now=NOW + timedelta(seconds=300))
        assert busy.is_material is True

    def test_summaries_are_rate_limited_rather_than_per_line(self) -> None:
        registry = self._registry()
        registry.observe(CAMERA_ID, "[rtsp @ 0x1] Empty H.264 RTP packet", now=NOW)
        assert registry.due_for_summary(CAMERA_ID, now=NOW) is True
        assert registry.due_for_summary(CAMERA_ID, now=NOW + timedelta(seconds=10)) is False
        later = NOW + timedelta(seconds=corruption.SUMMARY_INTERVAL_SECONDS + 1)
        assert registry.due_for_summary(CAMERA_ID, now=later) is True

    def test_ordinary_output_is_not_counted_as_corruption(self) -> None:
        registry = self._registry()
        assert registry.observe(CAMERA_ID, "", now=NOW) is None
        assert registry.observe(CAMERA_ID, "   ", now=NOW) is None

    def test_a_source_change_resets_the_evidence(self) -> None:
        registry = self._registry()
        registry.observe(CAMERA_ID, "[rtsp @ 0x1] Empty H.264 RTP packet", now=NOW)
        registry.reset(CAMERA_ID)
        assert registry.snapshot(CAMERA_ID, now=NOW).total == 0


class TestRecorderWriteWatch:
    async def test_a_running_process_with_no_recent_segment_is_restarted(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        manager = RecordingManager(settings)
        manager._root = tmp_path
        manager._segment_seconds = 60
        process = SimpleNamespace(
            is_running=True,
            started_at=datetime.now(UTC) - timedelta(minutes=3),
            restart=AsyncMock(),
        )
        manager._recorders = {CAMERA_ID: process}  # type: ignore[dict-item]

        await manager._restart_stalled_recorders()

        process.restart.assert_awaited_once_with()

    async def test_a_recent_segment_proves_the_recorder_is_live(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        manager = RecordingManager(settings)
        manager._root = tmp_path
        manager._segment_seconds = 60
        process = SimpleNamespace(
            is_running=True,
            started_at=datetime.now(UTC) - timedelta(minutes=3),
            restart=AsyncMock(),
        )
        manager._recorders = {CAMERA_ID: process}  # type: ignore[dict-item]
        current_hour = paths.hour_directory(tmp_path, CAMERA_ID, datetime.now(UTC))
        current_hour.mkdir(parents=True)
        (current_hour / "segment-current.mp4").write_bytes(b"active")

        await manager._restart_stalled_recorders()

        process.restart.assert_not_awaited()

    async def test_a_new_process_gets_the_full_startup_grace(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        manager = RecordingManager(settings)
        manager._root = tmp_path
        manager._segment_seconds = 60
        process = SimpleNamespace(
            is_running=True,
            started_at=datetime.now(UTC) - timedelta(seconds=30),
            restart=AsyncMock(),
        )
        manager._recorders = {CAMERA_ID: process}  # type: ignore[dict-item]

        await manager._restart_stalled_recorders()

        process.restart.assert_not_awaited()


class TestPerCameraSegmentDuration:
    def test_override_changes_only_that_recorder(self, settings: Settings) -> None:
        manager = RecordingManager(settings)
        manager._segment_seconds = 60
        overridden = Camera(recording_segment_seconds=30)
        inherited = Camera(recording_segment_seconds=None)

        assert manager._segment_seconds_for(overridden) == 30
        assert manager._segment_seconds_for(inherited) == 60


class TestPerCameraSourceMode:
    def test_manager_maps_relay_mode_to_a_strict_policy(self, settings: Settings) -> None:
        manager = RecordingManager(settings)
        camera = Camera(
            id=CAMERA_ID,
            main_stream_url="rtsp://camera/stream",
            recording_source_mode=RecordingSourceMode.RELAY,
        )
        policy = manager._policy_for(camera)

        assert policy.decision().source is RecorderSource.RELAY
        assert policy.decision().reason is SourceReason.RELAY_PINNED


class TestStorageRoot:
    def test_preparing_creates_and_marks_the_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "cctv"
        status = prepare_storage(root)
        assert status.available is True
        assert (root / MARKER_FILENAME).exists()
        assert status.total_bytes > 0

    def test_a_missing_directory_is_unavailable(self, tmp_path: Path) -> None:
        status = check_storage(tmp_path / "nope")
        assert status.available is False
        assert "does not exist" in (status.reason or "")

    def test_a_vanished_marker_means_the_mount_is_gone(self, tmp_path: Path) -> None:
        """The directory still exists and is writable, but the filesystem that
        was mounted on it is not. Writing here would fill the system disk."""
        root = tmp_path / "cctv"
        prepare_storage(root)
        (root / MARKER_FILENAME).unlink()

        status = check_storage(root)
        assert status.available is False
        assert "not mounted" in (status.reason or "")
        assert "suspended" in (status.reason or "").lower()

    def test_a_file_in_place_of_the_directory_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "cctv"
        target.write_text("not a directory")
        assert check_storage(target).available is False

    def test_preparing_never_removes_existing_content(self, tmp_path: Path) -> None:
        """Sentinel creates the directory; it must never clear or format it."""
        root = tmp_path / "cctv"
        root.mkdir()
        (root / "existing-footage.mp4").write_bytes(b"precious")

        prepare_storage(root)
        assert (root / "existing-footage.mp4").read_bytes() == b"precious"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_an_unwritable_directory_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "cctv"
        prepare_storage(root)
        root.chmod(0o500)
        try:
            assert check_storage(root).available is False
        finally:
            root.chmod(0o700)


@requires_ffmpeg
class TestRealSegmentPipeline:
    """Runs the real recorder arguments against a generated clip, then indexes
    the result. Anything FFmpeg would reject fails here rather than silently
    producing no footage on a live install."""

    @pytest.fixture
    def recorded_segments(self, tmp_path: Path) -> Path:
        assert FFMPEG
        source = tmp_path / "source.mp4"
        subprocess.run(  # noqa: S603 - fixed argument list
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x180:rate=15:duration=6",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "15",
                str(source),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )

        root = tmp_path / "recordings"
        pattern = recorder.prepare_output(root, CAMERA_ID)
        command = build_record_command(
            RecorderInput(url=str(source), via_go2rtc=False),
            pattern,
            ffmpeg_binary=FFMPEG,
            segment_seconds=2,
        )
        # Same arguments the recorder uses, with two input-side adjustments:
        # the RTSP flags do not apply to a local file, and -re paces the read at
        # native frame rate so -segment_atclocktime actually crosses a boundary.
        # Without -re FFmpeg consumes the clip in milliseconds and every frame
        # lands in the same clock second, producing one segment.
        for flag in ("-rtsp_transport", "-timeout"):
            index = command.index(flag)
            del command[index : index + 2]
        command.insert(command.index("-i"), "-re")

        subprocess.run(  # noqa: S603 - argument list built by the code under test
            command,
            check=True,
            capture_output=True,
            timeout=180,
            env={**os.environ, **recorder.build_recorder_environment()},
        )
        return root

    def test_ffmpeg_accepts_the_recorder_arguments(self, recorded_segments: Path) -> None:
        files = paths.iter_segment_files(recorded_segments, CAMERA_ID)
        assert len(files) >= 2, "expected the clip to be split into segments"
        assert all(path.stat().st_size > 1024 for path in files)

    def test_the_layout_matches_what_the_indexer_expects(self, recorded_segments: Path) -> None:
        for path in paths.iter_segment_files(recorded_segments, CAMERA_ID):
            relative = paths.relative_path(recorded_segments, path)
            assert paths.parse_segment_start(relative) is not None

    async def test_segments_are_indexed_with_real_metadata(
        self, settings: Settings, recorded_segments: Path, admin_client: AsyncClient
    ) -> None:
        del settings
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]

        # Re-home the recorded files under the camera that actually exists.
        shutil.move(str(recorded_segments / CAMERA_ID), str(recorded_segments / camera_id))

        async with session_scope() as session:
            result = await indexer.index_camera(
                session,
                camera_id,
                recorded_segments,
                now=datetime.now(UTC) + timedelta(minutes=5),
            )

        assert result.indexed >= 2
        assert result.damaged == 0

        listing = (await admin_client.get(f"/api/v1/recordings?camera_id={camera_id}")).json()
        assert listing["total"] >= 2
        first = listing["segments"][0]
        assert first["codec"] == "h264"
        assert first["width"] == 320
        assert first["height"] == 180
        assert first["duration_seconds"] > 0
        assert first["size_bytes"] > 1024
        assert first["status"] == "complete"
        assert first["protected"] is False

    async def test_a_downloaded_segment_is_the_real_file(
        self, settings: Settings, recorded_segments: Path, admin_client: AsyncClient
    ) -> None:
        del settings
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        camera_id = created["id"]
        shutil.move(str(recorded_segments / CAMERA_ID), str(recorded_segments / camera_id))

        async with session_scope() as session:
            await indexer.index_camera(
                session, camera_id, recorded_segments, now=datetime.now(UTC) + timedelta(minutes=5)
            )

        listing = (await admin_client.get(f"/api/v1/recordings?camera_id={camera_id}")).json()
        segment_id = listing["segments"][0]["id"]

        response = await admin_client.get(f"/api/v1/recordings/{segment_id}/download")
        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["accept-ranges"] == "bytes"
        assert len(response.content) > 1024
        assert b"ftyp" in response.content[:64]


class TestIndexer:
    def _write_segment(self, root: Path, camera_id: str, when: datetime, size: int) -> Path:
        directory = paths.hour_directory(root, camera_id, when)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"segment-{when:%H-%M-%S}.mp4"
        path.write_bytes(b"\x00" * size)
        return path

    async def test_a_tiny_file_is_recorded_as_damaged(
        self, settings: Settings, recordings_dir: Path, admin_client: AsyncClient
    ) -> None:
        """FFmpeg creates the file before the first keyframe arrives; something
        this small is a failed start, and the gap should be visible."""
        del settings
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        self._write_segment(
            recordings_dir, created["id"], datetime(2026, 8, 13, 15, 0, tzinfo=UTC), 10
        )

        async with session_scope() as session:
            result = await indexer.index_camera(
                session,
                created["id"],
                recordings_dir,
                now=datetime.now(UTC) + timedelta(minutes=5),
            )

        assert result.damaged == 1
        async with session_scope() as session:
            segment = (await session.execute(select(RecordingSegment))).scalar_one()
        assert segment.status is SegmentStatus.DAMAGED

    async def test_media_probing_never_holds_the_sqlite_writer_lock(
        self,
        settings: Settings,
        recordings_dir: Path,
        admin_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Indexer writes stay pending until its surrounding transaction commits."""
        del settings
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        self._write_segment(
            recordings_dir, created["id"], datetime(2026, 8, 13, 15, 0, tzinfo=UTC), 10
        )

        async with session_scope() as session:
            original_flush = session.flush
            explicit_flush = AsyncMock(
                side_effect=AssertionError("indexing flushed before media probing finished")
            )
            monkeypatch.setattr(session, "flush", explicit_flush)
            result = await indexer.index_camera(
                session,
                created["id"],
                recordings_dir,
                now=datetime.now(UTC) + timedelta(minutes=5),
            )
            monkeypatch.setattr(session, "flush", original_flush)

        assert result.damaged == 1
        explicit_flush.assert_not_awaited()

    async def test_an_unreadable_file_is_recorded_as_damaged_not_skipped(
        self, settings: Settings, recordings_dir: Path, admin_client: AsyncClient
    ) -> None:
        del settings
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        self._write_segment(
            recordings_dir, created["id"], datetime(2026, 8, 13, 15, 0, tzinfo=UTC), 50_000
        )

        async with session_scope() as session:
            result = await indexer.index_camera(
                session,
                created["id"],
                recordings_dir,
                now=datetime.now(UTC) + timedelta(minutes=5),
            )

        assert result.damaged == 1
        assert result.indexed == 0

    async def test_the_file_being_written_is_left_for_the_next_sweep(
        self, settings: Settings, recordings_dir: Path, admin_client: AsyncClient
    ) -> None:
        del settings
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        self._write_segment(
            recordings_dir, created["id"], datetime(2026, 8, 13, 15, 0, tzinfo=UTC), 50_000
        )

        async with session_scope() as session:
            # "now" is the moment the file was written, so it looks open.
            result = await indexer.index_camera(
                session, created["id"], recordings_dir, now=datetime.now(UTC)
            )
        assert result.skipped == 1
        assert result.indexed == 0
        assert result.damaged == 0

    async def test_indexing_is_idempotent(
        self, settings: Settings, recordings_dir: Path, admin_client: AsyncClient
    ) -> None:
        del settings
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        self._write_segment(
            recordings_dir, created["id"], datetime(2026, 8, 13, 15, 0, tzinfo=UTC), 50_000
        )
        later = datetime.now(UTC) + timedelta(minutes=5)

        async with session_scope() as session:
            await indexer.index_camera(session, created["id"], recordings_dir, now=later)
        async with session_scope() as session:
            second = await indexer.index_camera(session, created["id"], recordings_dir, now=later)

        assert second.indexed == 0
        assert second.damaged == 0
        async with session_scope() as session:
            count = len(list((await session.execute(select(RecordingSegment))).scalars()))
        assert count == 1

    async def test_a_deleted_file_is_marked_missing_rather_than_forgotten(
        self, settings: Settings, recordings_dir: Path, admin_client: AsyncClient
    ) -> None:
        del settings
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        path = self._write_segment(
            recordings_dir, created["id"], datetime(2026, 8, 13, 15, 0, tzinfo=UTC), 50_000
        )
        later = datetime.now(UTC) + timedelta(minutes=5)

        async with session_scope() as session:
            await indexer.index_camera(session, created["id"], recordings_dir, now=later)

        path.unlink()
        async with session_scope() as session:
            result = await indexer.index_camera(session, created["id"], recordings_dir, now=later)
        assert result.missing == 1

        async with session_scope() as session:
            segment = (await session.execute(select(RecordingSegment))).scalar_one()
        assert segment.status is SegmentStatus.MISSING

    async def test_stray_files_are_ignored(
        self, settings: Settings, recordings_dir: Path, admin_client: AsyncClient
    ) -> None:
        del settings
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        directory = recordings_dir / created["id"] / "2026" / "08" / "13" / "15"
        directory.mkdir(parents=True)
        (directory / "holiday-video.mp4").write_bytes(b"\x00" * 50_000)

        async with session_scope() as session:
            result = await indexer.index_camera(
                session,
                created["id"],
                recordings_dir,
                now=datetime.now(UTC) + timedelta(minutes=5),
            )
        assert result.indexed == 0
        assert result.skipped == 1


class TestRecordingsApi:
    async def _segment(self, camera_id: str, **overrides: object) -> RecordingSegment:
        defaults: dict[str, object] = {
            "camera_id": camera_id,
            "path": f"{camera_id}/2026/08/13/15/segment-15-00-00.mp4",
            "started_at": datetime(2026, 8, 13, 15, 0, tzinfo=UTC),
            "ended_at": datetime(2026, 8, 13, 15, 1, tzinfo=UTC),
            "duration_seconds": 60.0,
            "size_bytes": 4_000_000,
            "status": SegmentStatus.COMPLETE,
        }
        defaults.update(overrides)
        async with session_scope() as session:
            segment = RecordingSegment(**defaults)  # type: ignore[arg-type]
            session.add(segment)
            await session.flush()
            return segment

    async def test_listing_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/recordings")).status_code == 401

    async def test_the_on_disk_path_is_never_exposed(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await self._segment(created["id"])

        response = await admin_client.get("/api/v1/recordings")
        assert response.status_code == 200
        assert "path" not in response.json()["segments"][0]
        assert "segment-15-00-00.mp4" not in response.text

    async def test_missing_segments_are_hidden_from_the_listing(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await self._segment(created["id"], status=SegmentStatus.MISSING)
        assert (await admin_client.get("/api/v1/recordings")).json()["total"] == 0

    async def test_the_time_filter_includes_an_overlapping_segment(
        self, admin_client: AsyncClient
    ) -> None:
        """Asking for 15:00:30 must return the segment that started at 15:00 and
        is still running, not nothing."""
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        await self._segment(created["id"])

        response = await admin_client.get(
            "/api/v1/recordings",
            params={"start": "2026-08-13T15:00:30Z", "end": "2026-08-13T15:00:40Z"},
        )
        assert response.json()["total"] == 1

    async def test_a_segment_can_be_protected(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        segment = await self._segment(created["id"])

        response = await admin_client.patch(
            f"/api/v1/recordings/{segment.id}", json={"protected": True}
        )
        assert response.status_code == 200
        assert response.json()["protected"] is True

        filtered = await admin_client.get("/api/v1/recordings?protected=true")
        assert filtered.json()["total"] == 1

    async def test_unknown_fields_are_refused(self, admin_client: AsyncClient) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        segment = await self._segment(created["id"])
        response = await admin_client.patch(
            f"/api/v1/recordings/{segment.id}", json={"path": "../../etc/passwd"}
        )
        assert response.status_code == 422

    async def test_downloading_a_vanished_file_reports_gone(
        self, admin_client: AsyncClient
    ) -> None:
        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        segment = await self._segment(created["id"])
        response = await admin_client.get(f"/api/v1/recordings/{segment.id}/download")
        assert response.status_code == 410

    async def test_a_path_escaping_the_storage_root_is_refused(
        self, admin_client: AsyncClient, tmp_path: Path
    ) -> None:
        """A database restored from elsewhere, or a planted symlink, must not be
        able to serve an arbitrary file."""
        secret = tmp_path / "secret.txt"
        secret.write_text("classified")

        created = (await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)).json()
        segment = await self._segment(created["id"], path="../../../secret.txt")

        response = await admin_client.get(f"/api/v1/recordings/{segment.id}/download")
        assert response.status_code in (404, 410)
        assert "classified" not in response.text

    async def test_status_reports_storage(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/v1/recordings/status")
        assert response.status_code == 200
        body = response.json()
        assert body["storage_available"] is True
        assert body["suspended_reason"] is None
        assert body["total_bytes"] > 0


class TestRecordingHealth:
    async def test_recording_is_skipped_when_nothing_is_configured(
        self, client: AsyncClient
    ) -> None:
        body = (await client.get("/api/v1/system/health")).json()
        assert body["components"]["recording"]["state"] == "skipped"
        assert body["recording"] is True
        assert body["status"] == "healthy"

    async def test_recording_is_failed_when_a_camera_expects_it_but_nothing_runs(
        self, admin_client: AsyncClient
    ) -> None:
        await admin_client.post("/api/v1/cameras", json=CAMERA_PAYLOAD)
        body = (await admin_client.get("/api/v1/system/health")).json()
        assert body["components"]["recording"]["state"] == "failed"
        assert body["recording"] is False
        assert body["components"]["recording"]["data"]["expected"] == 1
