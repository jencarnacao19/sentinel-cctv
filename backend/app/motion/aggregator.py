"""Turning per-frame results into events.

A person walking across a driveway produces motion in most frames for ten
seconds, with brief gaps whenever they pass behind a car or the detector loses
them for a frame. Reporting each of those as an event would produce a list
nobody can read.

The state machine here does three things:

* **Requires confirmation to start.** A single noisy frame is not an event; a
  few consecutive ones are.
* **Tolerates gaps.** Motion is only considered over after a grace period with
  nothing, so one lost frame does not split an incident in two.
* **Applies roll.** The event's playback window starts before the movement and
  ends after it, because the interesting part is usually the approach.

Deliberately free of any I/O so it can be tested frame by frame at any speed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from app.motion.detector import MotionResult

#: Consecutive, spatially-related motion frames before an event is declared.
#: At the default 5 fps, the first-to-third span is about 0.4 seconds: long
#: enough to reject two unrelated compression/rain blobs while still catching
#: a person walking briskly through the view.
CONFIRM_FRAMES = 3


def regions_follow(
    previous: list[tuple[int, int, int, int]],
    current: list[tuple[int, int, int, int]],
) -> bool:
    """Whether at least one current region plausibly follows a previous one.

    Bounding boxes are expanded before intersection because a real object can
    move several pixels between 5 fps analysis frames. Unrelated noise on
    opposite sides of the image must not accumulate into a confirmed event.
    Empty regions mean the caller supplied only a score (not detector output),
    so compatibility is retained for integrations and pure state-machine tests.
    """
    if not previous or not current:
        return True
    for x, y, width, height in previous:
        margin_x = max(8, width // 2)
        margin_y = max(8, height // 2)
        left = x - margin_x
        top = y - margin_y
        right = x + width + margin_x
        bottom = y + height + margin_y
        for other_x, other_y, other_width, other_height in current:
            if (
                other_x < right
                and other_x + other_width > left
                and other_y < bottom
                and other_y + other_height > top
            ):
                return True
    return False


class Transition(StrEnum):
    NONE = "none"
    STARTED = "started"
    CONTINUING = "continuing"
    ENDED = "ended"


@dataclass(slots=True)
class MotionWindow:
    """One in-progress or just-finished event."""

    started_at: datetime
    last_motion_at: datetime
    peak_score: float = 0.0
    frames_with_motion: int = 0
    ended_at: datetime | None = None

    @property
    def duration_seconds(self) -> float:
        end = self.ended_at or self.last_motion_at
        return max(0.0, (end - self.started_at).total_seconds())


@dataclass(slots=True)
class AggregatorConfig:
    end_grace_seconds: float = 8.0
    """Motion must be absent this long before an event is closed. Movements
    closer together than this belong to the same incident."""
    pre_roll_seconds: float = 5.0
    post_roll_seconds: float = 10.0
    confirm_frames: int = CONFIRM_FRAMES
    max_event_seconds: float = 15 * 60
    """A hard ceiling. Rain on a lens or a swaying branch that defeats every
    filter must not produce a single event that lasts all night."""


@dataclass(slots=True)
class AggregatorOutcome:
    transition: Transition
    window: MotionWindow | None = None
    """The affected event, for STARTED, CONTINUING and ENDED."""
    score: float = 0.0
    is_peak: bool = False
    """True when this frame set a new peak, so the caller knows to replace the
    snapshot with a better one."""


@dataclass(slots=True)
class MotionAggregator:
    """Per-camera state machine over :class:`MotionResult` values."""

    config: AggregatorConfig = field(default_factory=AggregatorConfig)
    _window: MotionWindow | None = None
    _pending_since: datetime | None = None
    _pending_frames: int = 0
    _pending_regions: list[tuple[int, int, int, int]] = field(default_factory=list)

    @property
    def active_window(self) -> MotionWindow | None:
        return self._window

    def reset(self) -> None:
        self._window = None
        self._pending_since = None
        self._pending_frames = 0
        self._pending_regions = []

    def observe(self, result: MotionResult, now: datetime) -> AggregatorOutcome:
        """Feed one analysed frame; returns what changed, if anything."""
        if result.has_motion:
            return self._on_motion(result, now)
        return self._on_still(now)

    def _on_motion(self, result: MotionResult, now: datetime) -> AggregatorOutcome:
        if self._window is not None:
            window = self._window
            window.last_motion_at = now
            window.frames_with_motion += 1
            is_peak = result.score > window.peak_score
            if is_peak:
                window.peak_score = result.score

            if window.duration_seconds >= self.config.max_event_seconds:
                # Close it and immediately start a fresh one, so continuous
                # movement becomes a series of events rather than one endless.
                closed = self._close(now)
                self._begin(now, result.score)
                return AggregatorOutcome(
                    transition=Transition.ENDED, window=closed, score=result.score
                )

            return AggregatorOutcome(
                transition=Transition.CONTINUING,
                window=window,
                score=result.score,
                is_peak=is_peak,
            )

        # Not yet in an event: count consecutive motion frames.
        if self._pending_frames > 0 and not regions_follow(self._pending_regions, result.regions):
            # Start a new candidate rather than allowing unrelated motion on
            # different sides of the image to confirm one false incident.
            self._pending_frames = 1
            self._pending_since = now
            self._pending_regions = list(result.regions)
            return AggregatorOutcome(transition=Transition.NONE, score=result.score)

        self._pending_frames += 1
        self._pending_regions = list(result.regions)
        if self._pending_since is None:
            self._pending_since = now

        if self._pending_frames < self.config.confirm_frames:
            return AggregatorOutcome(transition=Transition.NONE, score=result.score)

        # Backdate to the first motion frame, not the confirming one, so the
        # event's timestamp is when the movement actually began.
        started_at = self._pending_since
        confirmed_frames = self._pending_frames
        self._begin(started_at, result.score)
        assert self._window is not None  # noqa: S101 - just assigned
        self._window.last_motion_at = now
        self._window.frames_with_motion = confirmed_frames
        return AggregatorOutcome(
            transition=Transition.STARTED,
            window=self._window,
            score=result.score,
            is_peak=True,
        )

    def _on_still(self, now: datetime) -> AggregatorOutcome:
        self._pending_frames = 0
        self._pending_since = None
        self._pending_regions = []

        window = self._window
        if window is None:
            return AggregatorOutcome(transition=Transition.NONE)

        idle = (now - window.last_motion_at).total_seconds()
        if idle < self.config.end_grace_seconds:
            # Still inside the grace period: this is a gap in one incident, not
            # the end of it.
            return AggregatorOutcome(transition=Transition.CONTINUING, window=window)

        closed = self._close(window.last_motion_at)
        return AggregatorOutcome(transition=Transition.ENDED, window=closed)

    def _begin(self, when: datetime, score: float) -> None:
        self._window = MotionWindow(
            started_at=when, last_motion_at=when, peak_score=score, frames_with_motion=1
        )
        self._pending_frames = 0
        self._pending_since = None
        self._pending_regions = []

    def _close(self, when: datetime) -> MotionWindow:
        window = self._window
        assert window is not None  # noqa: S101 - callers check
        window.ended_at = when
        self._window = None
        return window

    def flush(self, now: datetime) -> AggregatorOutcome:
        """Close any open event, for shutdown or a lost connection.

        Without this a camera that goes offline mid-movement leaves an event
        with no end time, which the timeline would draw as running forever.
        """
        if self._window is None:
            return AggregatorOutcome(transition=Transition.NONE)
        closed = self._close(min(now, self._window.last_motion_at))
        return AggregatorOutcome(transition=Transition.ENDED, window=closed)

    def playback_bounds(self, window: MotionWindow) -> tuple[datetime, datetime | None]:
        """``(pre_roll_start, post_roll_end)`` for a window."""
        start = window.started_at - timedelta(seconds=self.config.pre_roll_seconds)
        end = (
            window.ended_at + timedelta(seconds=self.config.post_roll_seconds)
            if window.ended_at
            else None
        )
        return start, end
