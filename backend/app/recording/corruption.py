"""Bounded, rate-limited accounting of what a recorder's input is doing wrong.

FFmpeg reports media damage per packet. On the reference installation that
meant 25,146 ``bad cseq`` lines, 13,102 ``Empty H.264 RTP packet`` lines and
5,229 ``non-existing PPS`` lines from a single camera in four hours -- roughly
one log line every 0.3 seconds, which is simultaneously far too much to read
and, because nothing counted it, not enough to act on. The interesting facts
were "this camera is corrupt" and "at what rate", and both were buried.

So the raw lines are counted, never stored. Each line is matched against a
closed pattern table, folded into a counter, and discarded. A summary is
emitted at most once per :data:`SUMMARY_INTERVAL_SECONDS` per camera, carrying
counts rather than text. The counters feed the Reliability Center, which is how
"Exterior is delivering damaged media" becomes a state an operator can see
instead of a log nobody reads.

Nothing in this module retains a fragment of FFmpeg's output. The input is
already redacted by the supervisor; the output is integers and enum members.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

#: At most one aggregated summary per camera per interval.
SUMMARY_INTERVAL_SECONDS = 300.0

#: Window over which a rate is computed.
RATE_WINDOW_SECONDS = 600.0

#: Hard cap on retained timestamps per camera. Ten minutes of a pathological
#: camera would otherwise be tens of thousands of datetimes; the rate is just
#: as usable when it saturates, and the memory stays flat.
MAX_TRACKED_EVENTS = 2048

#: Events per minute above which a camera counts as materially corrupt. Derived
#: from the reference data: healthy channels produced zero, while the failing
#: channel produced roughly 160 per minute. Anything above this is a stream
#: problem, not an occasional dropped packet.
CORRUPTION_RATE_THRESHOLD_PER_MINUTE = 6.0


class CorruptionKind(StrEnum):
    """What a line of recorder output was about. A closed vocabulary."""

    EMPTY_RTP_PACKET = "empty_rtp_packet"
    """The camera sent an RTP packet with no payload."""
    MISSING_PARAMETER_SET = "missing_parameter_set"
    """A frame referenced SPS/PPS the stream never delivered."""
    DECODE_ERROR = "decode_error"
    """The bitstream itself would not parse."""
    RTSP_DESYNC = "rtsp_desync"
    """RTSP responses arrived with unexpected sequence numbers."""
    TIMESTAMP = "timestamp"
    """Presentation timestamps were missing, duplicated or out of order."""
    CONNECTION = "connection"
    """The transport failed: refused, reset, timed out or unreachable."""
    MUXER = "muxer"
    """The output could not be opened or written."""
    OTHER = "other"
    """Recognised as an error, but not as one of the above."""


#: Ordered: first match wins, most specific first.
_PATTERNS: tuple[tuple[CorruptionKind, re.Pattern[str]], ...] = (
    (
        CorruptionKind.EMPTY_RTP_PACKET,
        re.compile(r"empty\s+h\.?26[45]\s+rtp\s+packet|empty\s+rtp\s+packet", re.IGNORECASE),
    ),
    (
        CorruptionKind.MISSING_PARAMETER_SET,
        re.compile(
            r"non[- ]?existing\s+(?:pps|sps)"
            r"|(?:pps|sps)\b[^.]{0,32}\b(?:missing|not\s+(?:yet\s+)?available)"
            r"|no\s+frame\s*!"
            r"|could\s+not\s+find\s+codec\s+parameters",
            re.IGNORECASE,
        ),
    ),
    (
        CorruptionKind.RTSP_DESYNC,
        re.compile(r"bad\s+cseq|method\s+\w+\s+failed|rtsp:\s*(?:bad|malformed)", re.IGNORECASE),
    ),
    (
        CorruptionKind.DECODE_ERROR,
        re.compile(
            r"decode_slice_header\s+error"
            r"|error\s+while\s+decoding\s+mb"
            r"|corrupt(?:ed)?\s+(?:packet|macroblock|frame)"
            r"|concealing\s+\d+\s+dc",
            re.IGNORECASE,
        ),
    ),
    (
        CorruptionKind.MUXER,
        re.compile(
            r"could\s+not\s+write\s+header"
            r"|error\s+initializing\s+output\s+stream"
            r"|incorrect\s+codec\s+parameters",
            re.IGNORECASE,
        ),
    ),
    (
        CorruptionKind.CONNECTION,
        re.compile(
            r"connection\s+(?:refused|reset|timed\s+out)"
            r"|no\s+route\s+to\s+host"
            r"|network\s+is\s+unreachable"
            r"|input/output\s+error"
            r"|error\s+opening\s+input"
            r"|immediate\s+exit\s+requested",
            re.IGNORECASE,
        ),
    ),
    (
        CorruptionKind.TIMESTAMP,
        re.compile(
            r"non[- ]monotonic(?:ally\s+increasing)?\s+dts"
            r"|timestamps\s+are\s+unset"
            r"|pts\s+has\s+no\s+value"
            r"|invalid\s+(?:dts|pts)",
            re.IGNORECASE,
        ),
    ),
)

#: Lines that are noise rather than a distinct event, so they are not counted
#: twice. FFmpeg collapses repeats itself and says so.
_REPEAT_NOTICE = re.compile(r"last\s+message\s+repeated\s+(\d+)\s+times", re.IGNORECASE)


def classify(line: str) -> CorruptionKind | None:
    """Categorise one line of recorder output, or ``None`` if it is not an event.

    Returns an enum member only. No part of ``line`` is returned, retained or
    logged by this function.
    """
    if not line or not line.strip():
        return None
    for kind, pattern in _PATTERNS:
        if pattern.search(line):
            return kind
    return CorruptionKind.OTHER


def repeat_count(line: str) -> int:
    """How many suppressed repeats a ``Last message repeated N times`` line means."""
    match = _REPEAT_NOTICE.search(line)
    if match is None:
        return 0
    try:
        return max(int(match.group(1)), 0)
    except ValueError:
        return 0


@dataclass(frozen=True, slots=True)
class CorruptionSnapshot:
    """What the Reliability Center reads. Counts and rates, never text."""

    total: int
    counts: dict[CorruptionKind, int]
    events_per_minute: float
    dominant: CorruptionKind | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None

    @property
    def is_material(self) -> bool:
        """Whether the rate is high enough to call the stream corrupt."""
        return self.events_per_minute >= CORRUPTION_RATE_THRESHOLD_PER_MINUTE


@dataclass(slots=True)
class CameraCorruption:
    """Rolling counters for one camera's recorder output."""

    counts: dict[CorruptionKind, int] = field(default_factory=dict)
    total: int = 0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    #: Recent event times, capped, for the rate. Older entries are evicted.
    _events: deque[datetime] = field(default_factory=lambda: deque(maxlen=MAX_TRACKED_EVENTS))
    _last_summary_at: datetime | None = None

    def record(self, kind: CorruptionKind, *, now: datetime, weight: int = 1) -> None:
        if weight <= 0:
            return
        self.counts[kind] = self.counts.get(kind, 0) + weight
        self.total += weight
        if self.first_seen_at is None:
            self.first_seen_at = now
        self.last_seen_at = now
        # A repeat notice stands for many suppressed lines at approximately
        # this moment. The deque already has a hard maxlen, so crediting those
        # repetitions to the rolling rate remains bounded while avoiding a
        # dangerous undercount (5,000 repeated errors previously looked like
        # one event for the auto-profile and reliability thresholds).
        self._events.extend([now] * min(weight, MAX_TRACKED_EVENTS))

    def snapshot(self, *, now: datetime) -> CorruptionSnapshot:
        self._evict(now)
        window_minutes = RATE_WINDOW_SECONDS / 60.0
        rate = len(self._events) / window_minutes if window_minutes > 0 else 0.0
        dominant = max(self.counts, key=lambda k: self.counts[k]) if self.counts else None
        return CorruptionSnapshot(
            total=self.total,
            counts=dict(self.counts),
            events_per_minute=rate,
            dominant=dominant,
            first_seen_at=self.first_seen_at,
            last_seen_at=self.last_seen_at,
        )

    def due_for_summary(self, *, now: datetime) -> bool:
        """Whether a summary may be logged, resetting the interval if so."""
        if not self._events:
            return False
        if self._last_summary_at is not None and (now - self._last_summary_at) < timedelta(
            seconds=SUMMARY_INTERVAL_SECONDS
        ):
            return False
        self._last_summary_at = now
        return True

    def reset(self) -> None:
        """Forget everything. Used when a camera's source or profile changes."""
        self.counts.clear()
        self.total = 0
        self.first_seen_at = None
        self.last_seen_at = None
        self._events.clear()
        self._last_summary_at = None

    def _evict(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=RATE_WINDOW_SECONDS)
        while self._events and self._events[0] < cutoff:
            self._events.popleft()


class CorruptionRegistry:
    """Per-camera counters, created on demand and dropped with the camera."""

    def __init__(self) -> None:
        self._cameras: dict[str, CameraCorruption] = {}

    def observe(self, camera_id: str, line: str, *, now: datetime) -> CorruptionKind | None:
        """Fold one line of recorder output in. Returns its category, if any."""
        repeats = repeat_count(line)
        entry = self._cameras.setdefault(camera_id, CameraCorruption())
        if repeats:
            # "Last message repeated N times" continues whatever came before,
            # so it is attributed to the dominant category rather than dropped.
            previous = entry.snapshot(now=now).dominant
            if previous is not None:
                entry.record(previous, now=now, weight=repeats)
            return previous
        kind = classify(line)
        if kind is None:
            return None
        entry.record(kind, now=now)
        return kind

    def snapshot(self, camera_id: str, *, now: datetime) -> CorruptionSnapshot:
        entry = self._cameras.get(camera_id)
        if entry is None:
            return CorruptionSnapshot(
                total=0,
                counts={},
                events_per_minute=0.0,
                dominant=None,
                first_seen_at=None,
                last_seen_at=None,
            )
        return entry.snapshot(now=now)

    def due_for_summary(self, camera_id: str, *, now: datetime) -> bool:
        entry = self._cameras.get(camera_id)
        return entry.due_for_summary(now=now) if entry is not None else False

    def reset(self, camera_id: str) -> None:
        entry = self._cameras.get(camera_id)
        if entry is not None:
            entry.reset()

    def forget(self, camera_id: str) -> None:
        self._cameras.pop(camera_id, None)

    def prune(self, camera_ids: set[str]) -> None:
        for camera_id in [key for key in self._cameras if key not in camera_ids]:
            del self._cameras[camera_id]
