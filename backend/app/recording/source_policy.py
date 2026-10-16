"""Which input a recorder reads from, decided by measurement rather than by turns.

The problem this replaces
------------------------

The previous :class:`RecorderInputSelector` alternated on *every* abnormal exit:
relay fails once, try direct; direct fails once, try relay; repeat for ever. On
the reference installation that produced 16 source switches on one camera in
four hours and drifted six of eight cameras onto a direct connection, because a
flip was never required to prove anything before the next flip undid it.

That drift is not cosmetic. go2rtc exists so that one connection to a camera
fans out to live view, motion detection and recording. A recorder on the direct
path opens a *second* RTSP session to a camera that go2rtc is already holding
open, and consumer cameras and NVR channels cap concurrent sessions at two or
three. The observed consequence was ``Connection timed out`` and
``Connection reset by peer`` on the direct path and RTSP ``bad cseq`` storms on
the shared NVR -- failures *caused by the alternation itself*, which then drove
more alternation.

The policy here
---------------

* **The relay is home.** It is the shared fan-out, so it is preferred whenever
  it works. Direct is a fallback, never a destination reached by accident.
* **A source must earn a switch.** Only after :data:`SWITCH_AFTER_FAILURES`
  consecutive failures on the active source is an alternate considered.
* **A source must earn its keep.** A run shorter than :data:`QUALIFY_SECONDS`
  is a failed qualification, which blocks that source for an exponentially
  growing cooldown. A run longer than it clears the source's penalties.
* **Switching has a floor.** :data:`SWITCH_COOLDOWN_SECONDS` between switches
  bounds the worst case to a handful of moves an hour instead of one per exit.
* **Home is retried, not abandoned.** When the relay's cooldown expires the
  camera returns to it to requalify, so one bad hour does not pin a camera to
  the expensive path for ever.

Every decision carries a :class:`SourceReason` so an administrator can be told
which path is active and *why*, and :meth:`SourcePolicy.reset` re-runs the whole
qualification from scratch on request.

This module is pure: it holds no URL, no credential and performs no I/O. The
manager feeds it outcomes and asks it for a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.database.models import RecorderSource

#: Consecutive failures on the active source before an alternate is considered.
#: One is far too eager -- a camera reboot, a go2rtc restart or a transient LAN
#: drop all produce a single failure and are all self-healing.
SWITCH_AFTER_FAILURES = 3

#: A run at least this long counts as a successful qualification. One complete
#: default segment proves that the source produced usable footage rather than
#: merely opening. Some consumer RTSP servers deliberately close otherwise
#: healthy sessions between 60 and 120 seconds; requiring several uninterrupted
#: segments would punish those cameras and grow their source cooldown forever.
QUALIFY_SECONDS = 60.0

#: Minimum wall-clock gap between two source switches for one camera. This is
#: the hysteresis: without it, two sources that both fail immediately would
#: still ping-pong at the supervisor's restart rate.
SWITCH_COOLDOWN_SECONDS = 300.0

#: First cooldown applied to a source that failed qualification, doubling per
#: consecutive failed qualification up to :data:`MAX_BLOCK_SECONDS`.
BASE_BLOCK_SECONDS = 300.0
MAX_BLOCK_SECONDS = 3600.0

#: How many consecutive failed qualifications are remembered per source. Beyond
#: this the cooldown is already at its maximum, so counting further only risks
#: an overflow in a persisted integer.
MAX_TRACKED_QUALIFICATION_FAILURES = 8


class SourceReason(StrEnum):
    """Why the current source is in use. A closed, viewer-safe vocabulary."""

    RELAY_PREFERRED = "relay_preferred"
    """The shared local relay is working; this is the healthy steady state."""
    RELAY_REQUALIFYING = "relay_requalifying"
    """Returning to the relay after its cooldown to see whether it recovered."""
    RELAY_UNAVAILABLE = "relay_unavailable"
    """The relay failed qualification, so a direct connection is in use."""
    DIRECT_QUALIFIED = "direct_qualified"
    """A direct connection has proven stable and the relay has not recovered."""
    DIRECT_ONLY = "direct_only"
    """Relay recording is switched off, or no relay exists for this install."""
    RELAY_ONLY = "relay_only"
    """No usable direct URL, so the relay is the only candidate."""
    RELAY_PINNED = "relay_pinned"
    """The administrator pinned this camera to the shared local relay."""
    DIRECT_PINNED = "direct_pinned"
    """The administrator pinned this camera to its direct RTSP source."""
    OPERATOR_RESET = "operator_reset"
    """An administrator cleared the learned state; qualification restarts."""


REASON_LABELS: dict[SourceReason, str] = {
    SourceReason.RELAY_PREFERRED: "Using the shared local relay (preferred).",
    SourceReason.RELAY_REQUALIFYING: "Retrying the shared local relay after a cooldown.",
    SourceReason.RELAY_UNAVAILABLE: (
        "The shared local relay failed repeatedly, so this camera is recorded over a "
        "direct connection."
    ),
    SourceReason.DIRECT_QUALIFIED: "A direct connection has proven stable for this camera.",
    SourceReason.DIRECT_ONLY: "Relay recording is switched off for this installation.",
    SourceReason.RELAY_ONLY: "No usable direct stream URL, so the relay is the only option.",
    SourceReason.RELAY_PINNED: "Pinned to the shared local relay by camera configuration.",
    SourceReason.DIRECT_PINNED: "Pinned to the direct camera stream by camera configuration.",
    SourceReason.OPERATOR_RESET: "An administrator reset the learned source; requalifying.",
}


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """What one recorder run achieved, as the manager observed it."""

    source: RecorderSource
    uptime_seconds: float
    return_code: int | None
    #: False for a deliberate stop (shutdown, privacy, operator restart), which
    #: must never be counted against a source.
    counts_as_failure: bool


@dataclass(frozen=True, slots=True)
class SourceDecision:
    """The source to use next, and the explanation an administrator sees."""

    source: RecorderSource
    reason: SourceReason
    changed: bool
    """Whether this decision differs from the previously active source."""

    @property
    def label(self) -> str:
        return REASON_LABELS[self.reason]


@dataclass(slots=True)
class CameraSourceState:
    """Per-camera qualification state. Serialisable, and holds no secret."""

    active: RecorderSource = RecorderSource.RELAY
    reason: SourceReason = SourceReason.RELAY_PREFERRED
    consecutive_failures: int = 0
    qualified: bool = False
    last_switch_at: datetime | None = None
    #: Earliest moment each source may be selected again, by source value.
    blocked_until: dict[str, datetime] = field(default_factory=dict)
    #: Consecutive failed qualifications per source, driving the cooldown.
    failed_qualifications: dict[str, int] = field(default_factory=dict)
    #: Switches made since the state was last reset, for the admin panel.
    switch_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active.value,
            "reason": self.reason.value,
            "consecutive_failures": self.consecutive_failures,
            "qualified": self.qualified,
            "last_switch_at": self.last_switch_at.isoformat() if self.last_switch_at else None,
            "blocked_until": {
                key: value.isoformat() for key, value in sorted(self.blocked_until.items())
            },
            "failed_qualifications": dict(sorted(self.failed_qualifications.items())),
            "switch_count": self.switch_count,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> CameraSourceState:
        """Decode persisted state, falling back to a clean state on anything odd.

        A hand-edited or partially written row must never stop a camera from
        recording, so every field is validated independently and a bad one is
        simply dropped.
        """
        state = cls()
        if not isinstance(raw, dict):
            return state
        state.active = _decode_source(raw.get("active")) or RecorderSource.RELAY
        state.reason = _decode_reason(raw.get("reason")) or SourceReason.RELAY_PREFERRED
        state.consecutive_failures = _decode_int(raw.get("consecutive_failures"))
        state.qualified = raw.get("qualified") is True
        state.last_switch_at = _decode_time(raw.get("last_switch_at"))
        blocked = raw.get("blocked_until")
        if isinstance(blocked, dict):
            for key, value in blocked.items():
                if _decode_source(key) is None:
                    continue
                moment = _decode_time(value)
                if moment is not None:
                    state.blocked_until[str(key)] = moment
        failures = raw.get("failed_qualifications")
        if isinstance(failures, dict):
            for key, value in failures.items():
                if _decode_source(key) is None:
                    continue
                state.failed_qualifications[str(key)] = min(
                    _decode_int(value), MAX_TRACKED_QUALIFICATION_FAILURES
                )
        state.switch_count = _decode_int(raw.get("switch_count"))
        return state


def _decode_source(value: Any) -> RecorderSource | None:
    if isinstance(value, RecorderSource):
        return value
    if isinstance(value, str):
        try:
            return RecorderSource(value)
        except ValueError:
            return None
    return None


def _decode_reason(value: Any) -> SourceReason | None:
    if isinstance(value, SourceReason):
        return value
    if isinstance(value, str):
        try:
            return SourceReason(value)
        except ValueError:
            return None
    return None


def _decode_int(value: Any) -> int:
    return max(int(value), 0) if isinstance(value, int) and not isinstance(value, bool) else 0


def _decode_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            decoded = datetime.fromisoformat(value)
        except ValueError:
            return None
        # Persisted policy state is normally written as an aware UTC value,
        # but a hand-edited/older row may be naive. Mixing it with the aware
        # ``now`` used by the manager raises during subtraction and would stop
        # the recorder from building its next command. Treat a naive value as
        # UTC and normalise every other offset to UTC at this boundary.
        if decoded.tzinfo is None:
            return decoded.replace(tzinfo=UTC)
        return decoded.astimezone(UTC)
    return None


def block_seconds(failures: int) -> float:
    """Cooldown for a source after ``failures`` consecutive failed qualifications."""
    if failures <= 0:
        return 0.0
    exponent = min(failures, MAX_TRACKED_QUALIFICATION_FAILURES) - 1
    return min(BASE_BLOCK_SECONDS * (2.0**exponent), MAX_BLOCK_SECONDS)


class SourcePolicy:
    """Decides one camera's recorder input from observed outcomes.

    ``relay_available`` and ``direct_available`` describe what this installation
    can offer at all: an install with ``record_via_go2rtc`` off has no relay,
    and a camera whose stream URL cannot be resolved has no direct path.
    """

    def __init__(
        self,
        *,
        relay_available: bool,
        direct_available: bool,
        state: CameraSourceState | None = None,
        pinned_source: RecorderSource | None = None,
    ) -> None:
        self._relay_available = relay_available
        self._direct_available = direct_available
        self._pinned_source = pinned_source
        self.state = state or CameraSourceState()
        self._reconcile_availability()

    # --- Availability ----------------------------------------------------

    def _reconcile_availability(self) -> None:
        """Force the active source onto something this install can actually use."""
        if self._pinned_source is not None:
            changed = self.state.active is not self._pinned_source
            self.state.active = self._pinned_source
            self.state.reason = self._pinned_reason
            if changed:
                self.state.qualified = False
                self.state.consecutive_failures = 0
            return
        if not self._relay_available and not self._direct_available:
            return
        if self.state.active is RecorderSource.RELAY and not self._relay_available:
            self.state.active = RecorderSource.DIRECT
            self.state.reason = SourceReason.DIRECT_ONLY
            self.state.qualified = False
            self.state.consecutive_failures = 0
        elif self.state.active is RecorderSource.DIRECT and not self._direct_available:
            self.state.active = RecorderSource.RELAY
            self.state.reason = SourceReason.RELAY_ONLY
            self.state.qualified = False
            self.state.consecutive_failures = 0

    @property
    def _preferred(self) -> RecorderSource:
        """Home. The relay unless this install has none."""
        if self._pinned_source is not None:
            return self._pinned_source
        return RecorderSource.RELAY if self._relay_available else RecorderSource.DIRECT

    @property
    def _pinned_reason(self) -> SourceReason:
        return (
            SourceReason.RELAY_PINNED
            if self._pinned_source is RecorderSource.RELAY
            else SourceReason.DIRECT_PINNED
        )

    def _alternate(self, source: RecorderSource) -> RecorderSource | None:
        if self._pinned_source is not None:
            return None
        other = RecorderSource.DIRECT if source is RecorderSource.RELAY else RecorderSource.RELAY
        available = (
            self._relay_available if other is RecorderSource.RELAY else (self._direct_available)
        )
        return other if available else None

    def _blocked(self, source: RecorderSource, now: datetime) -> bool:
        until = self.state.blocked_until.get(source.value)
        return until is not None and until > now

    # --- Reporting -------------------------------------------------------

    def decision(self) -> SourceDecision:
        return SourceDecision(source=self.state.active, reason=self.state.reason, changed=False)

    def blocked_for_seconds(self, source: RecorderSource, now: datetime) -> float:
        until = self.state.blocked_until.get(source.value)
        if until is None or until <= now:
            return 0.0
        return (until - now).total_seconds()

    # --- Transitions -----------------------------------------------------

    def reset(self, now: datetime) -> SourceDecision:
        """Clear every learned penalty and return home. The manual requalify."""
        state = self.state
        previous = state.active

        # Mutate the shared state object rather than replacing it. The manager
        # stores this object in its persistence map and the running recorder's
        # command closure holds a policy view over the same object. Replacing it
        # here would make the endpoint report success while both the persisted
        # map and the active recorder continued using the old penalties.
        state.active = self._preferred
        state.reason = SourceReason.OPERATOR_RESET
        state.consecutive_failures = 0
        state.qualified = False
        state.last_switch_at = now
        state.blocked_until.clear()
        state.failed_qualifications.clear()
        state.switch_count = 0
        self._reconcile_availability()
        return SourceDecision(
            source=state.active,
            reason=state.reason,
            changed=state.active is not previous,
        )

    def record_start(self, now: datetime) -> SourceDecision:
        """Called before a recorder starts, so a due requalification can happen.

        This is what stops a camera being pinned to the expensive direct path
        after one bad hour: as soon as the relay's cooldown has elapsed, the
        next start goes home to find out whether it recovered.
        """
        state = self.state
        preferred = self._preferred
        if (
            state.active is not preferred
            and not self._blocked(preferred, now)
            and self._can_switch(now)
        ):
            return self._switch_to(preferred, SourceReason.RELAY_REQUALIFYING, now)
        return self.decision()

    def record_outcome(self, outcome: SourceOutcome, now: datetime) -> SourceDecision:
        """Fold one finished recorder run into the policy and decide what is next."""
        state = self.state

        if outcome.source is not state.active:
            # A run of a source we have since moved away from. Its result says
            # nothing about the current choice.
            return self.decision()

        if not outcome.counts_as_failure:
            # A deliberate stop. Long uptime still counts as qualification --
            # a recorder an operator restarted after six clean hours has
            # certainly proven its source.
            if outcome.uptime_seconds >= QUALIFY_SECONDS:
                self._mark_qualified(outcome.source)
            return self.decision()

        if outcome.uptime_seconds >= QUALIFY_SECONDS:
            # It ran long enough to be considered good, then died. That is the
            # first failure of a new incident, not a continuation of an old
            # ladder, and the source keeps its qualification.
            self._mark_qualified(outcome.source)
            state.consecutive_failures = 1
            return self.decision()

        state.consecutive_failures += 1
        state.qualified = False
        if state.consecutive_failures < SWITCH_AFTER_FAILURES:
            return self.decision()

        # The active source has now failed qualification enough times to be
        # penalised and, if there is somewhere to go, abandoned.
        self._penalise(outcome.source, now)
        alternate = self._alternate(outcome.source)
        # A trial return to the preferred relay is deliberately allowed only
        # after its cooldown. If that trial then fails qualification, roll back
        # to the already proven direct source without waiting through a second
        # five-minute switch floor. The floor still applies to every ordinary
        # transition, so this exception cannot reintroduce blind ping-pong.
        failed_requalification = state.reason is SourceReason.RELAY_REQUALIFYING
        if (
            alternate is None
            or self._blocked(alternate, now)
            or (not failed_requalification and not self._can_switch(now))
        ):
            # Nowhere better to go. Keep retrying here rather than stopping:
            # the supervisor's backoff already paces the attempts, and a camera
            # that comes back must reconnect by itself.
            return self.decision()

        reason = (
            SourceReason.RELAY_UNAVAILABLE
            if alternate is RecorderSource.DIRECT
            else SourceReason.RELAY_REQUALIFYING
        )
        return self._switch_to(alternate, reason, now)

    def _can_switch(self, now: datetime) -> bool:
        last = self.state.last_switch_at
        if last is None:
            return True
        return (now - last).total_seconds() >= SWITCH_COOLDOWN_SECONDS

    def _switch_to(
        self, source: RecorderSource, reason: SourceReason, now: datetime
    ) -> SourceDecision:
        state = self.state
        changed = source is not state.active
        state.active = source
        state.reason = reason
        state.consecutive_failures = 0
        state.qualified = False
        if changed:
            state.last_switch_at = now
            state.switch_count += 1
        return SourceDecision(source=source, reason=reason, changed=changed)

    def _mark_qualified(self, source: RecorderSource) -> None:
        """A source that stayed up long enough starts again with a clean sheet."""
        state = self.state
        state.qualified = True
        state.consecutive_failures = 0
        state.failed_qualifications.pop(source.value, None)
        state.blocked_until.pop(source.value, None)
        if source is self._pinned_source:
            state.reason = self._pinned_reason
        elif source is self._preferred:
            state.reason = (
                SourceReason.RELAY_PREFERRED
                if source is RecorderSource.RELAY
                else SourceReason.DIRECT_ONLY
            )
        elif source is RecorderSource.DIRECT:
            state.reason = SourceReason.DIRECT_QUALIFIED

    def _penalise(self, source: RecorderSource, now: datetime) -> None:
        state = self.state
        failures = min(
            state.failed_qualifications.get(source.value, 0) + 1,
            MAX_TRACKED_QUALIFICATION_FAILURES,
        )
        state.failed_qualifications[source.value] = failures
        state.blocked_until[source.value] = now + timedelta(seconds=block_seconds(failures))


def prune_states(
    states: dict[str, CameraSourceState], camera_ids: set[str]
) -> dict[str, CameraSourceState]:
    """Drop state for cameras that no longer exist, so the row stays bounded."""
    return {key: value for key, value in states.items() if key in camera_ids}


def decode_states(raw: Any) -> dict[str, CameraSourceState]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): CameraSourceState.from_dict(value)
        for key, value in raw.items()
        if isinstance(key, str)
    }


def encode_states(states: dict[str, CameraSourceState]) -> dict[str, Any]:
    return {key: value.to_dict() for key, value in sorted(states.items())}
