"""In-process publish/subscribe for subsystem events.

Recorders, motion workers and the storage sweeper announce what happened; they
do not know, and must not care, whether anything is listening. Today the only
subscriber is the WebSocket endpoint, tomorrow it might also be a webhook --
neither should be able to slow down or break the code that published.

Two properties make that true:

* **Publishing never blocks and never raises.** A publisher is usually a worker
  in the middle of handling a frame; an exception there would be a genuine
  outage caused by a reporting concern.
* **Every subscriber queue is bounded.** A browser tab on a suspended laptop
  stops reading while its socket stays open. Dropping its oldest events costs a
  fixed amount of memory; queueing them without limit costs all of it.

Every event carries a sequence number from one counter, so a client that
reconnects can tell whether it missed anything -- and a small ring buffer of
recent events lets it catch up when the gap is short. The counter restarts from
zero when the process does, which clients detect as a sequence that went
backwards.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal, get_args

from app.logging_config import get_logger
from app.security.redaction import SENSITIVE_KEYS, redact_text

logger = get_logger("SYSTEM")

EventName = Literal[
    "camera.online",
    "camera.offline",
    "motion.started",
    "motion.ended",
    "recording.started",
    "recording.stopped",
    "storage.warning",
    "storage.critical",
    "camera.added",
    "camera.removed",
]

EVENT_NAMES: Final[tuple[EventName, ...]] = get_args(EventName)

#: Named constants so a publisher cannot silently typo an event into oblivion.
CAMERA_ONLINE: Final[EventName] = "camera.online"
CAMERA_OFFLINE: Final[EventName] = "camera.offline"
CAMERA_ADDED: Final[EventName] = "camera.added"
CAMERA_REMOVED: Final[EventName] = "camera.removed"
MOTION_STARTED: Final[EventName] = "motion.started"
MOTION_ENDED: Final[EventName] = "motion.ended"
RECORDING_STARTED: Final[EventName] = "recording.started"
RECORDING_STOPPED: Final[EventName] = "recording.stopped"
STORAGE_WARNING: Final[EventName] = "storage.warning"
STORAGE_CRITICAL: Final[EventName] = "storage.critical"

#: Per-subscriber backlog. Deep enough to absorb a burst from every camera at
#: once, shallow enough that a hundred stalled sockets are still megabytes.
DEFAULT_QUEUE_SIZE: Final = 256

#: Recent events kept for reconnecting clients. A browser that drops for a few
#: seconds resumes exactly; one that was away longer is told to resynchronise
#: rather than being handed a plausible-looking but incomplete history.
DEFAULT_HISTORY_SIZE: Final = 256

#: Guard against a publisher passing a self-referential structure.
_MAX_PAYLOAD_DEPTH: Final = 8


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened, as it will reach a subscriber."""

    seq: int
    name: str
    ts: datetime
    data: dict[str, Any] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        """The wire form. Every field is JSON-native by construction."""
        return {
            "type": self.name,
            "seq": self.seq,
            "ts": self.ts.isoformat().replace("+00:00", "Z"),
            "data": self.data,
        }


def _clean(value: Any, depth: int = 0) -> Any:
    """Make a payload safe to broadcast and safe to serialise.

    Two jobs, deliberately in one pass. Keys that could carry a credential are
    dropped outright rather than masked -- a subscriber has no use for the fact
    that a password existed. Anything that is not JSON-native becomes a string
    here, where the failure is a slightly ugly value, rather than inside
    ``send_json`` on a live socket, where it would kill the connection.
    """
    if depth > _MAX_PAYLOAD_DEPTH:
        return None
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _clean(item, depth + 1)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_clean(item, depth + 1) for item in value]
    return redact_text(str(value))


class Subscription:
    """One listener's queue of events.

    Bounded on purpose. When it fills the *oldest* event is discarded, not the
    newest: a slow listener is better served by current state plus a visible gap
    in the sequence numbers than by a faithful but ever-growing backlog.
    """

    __slots__ = ("_names", "_queue", "dropped")

    def __init__(
        self,
        *,
        maxsize: int = DEFAULT_QUEUE_SIZE,
        names: Iterable[str] | None = None,
    ) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        #: ``None`` means everything; an empty set means nothing, which is what
        #: a client asking only for event types this build does not know gets.
        self._names: frozenset[str] | None = None if names is None else frozenset(names)
        self.dropped = 0

    def wants(self, event: Event) -> bool:
        return self._names is None or event.name in self._names

    def deliver(self, event: Event) -> None:
        """Enqueue without blocking. Called by the bus only."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(event)

    async def get(self) -> Event:
        return await self._queue.get()

    @property
    def pending(self) -> int:
        return self._queue.qsize()


class EventBus:
    """The bus itself. One instance per process; see :func:`get_bus`.

    Not thread-safe, and deliberately so: everything in Sentinel that publishes
    runs on the event loop, and a lock here would put contention on the hot path
    of the motion workers for no benefit.
    """

    def __init__(self, *, history_size: int = DEFAULT_HISTORY_SIZE) -> None:
        self._sequence = 0
        self._subscribers: set[Subscription] = set()
        self._history: deque[Event] = deque(maxlen=history_size)

    # --- Publishing ------------------------------------------------------

    @property
    def sequence(self) -> int:
        """Sequence number of the most recent event; 0 before anything happens."""
        return self._sequence

    def publish(self, event_name: str, payload: Mapping[str, Any] | None = None) -> Event | None:
        """Announce an event. Returns the published event, or ``None`` if it
        was rejected or something went wrong -- never raises either way."""
        try:
            return self._publish(event_name, payload)
        except Exception:
            logger.warning(
                "Event could not be published", extra={"event": event_name}, exc_info=True
            )
            return None

    def _publish(self, event_name: str, payload: Mapping[str, Any] | None) -> Event | None:
        if event_name not in EVENT_NAMES:
            # A typo here would otherwise be invisible: nothing subscribes to a
            # name that does not exist, so the event would simply never arrive.
            logger.warning("Unknown event name was not published", extra={"event": event_name})
            return None

        self._sequence += 1
        event = Event(
            seq=self._sequence,
            name=event_name,
            ts=datetime.now(UTC),
            data=_clean(payload or {}),
        )
        self._history.append(event)

        for subscriber in tuple(self._subscribers):
            if subscriber.wants(event):
                subscriber.deliver(event)
        return event

    # --- Subscribing -----------------------------------------------------

    def subscribe(
        self,
        *,
        maxsize: int = DEFAULT_QUEUE_SIZE,
        names: Iterable[str] | None = None,
    ) -> Subscription:
        subscriber = Subscription(maxsize=maxsize, names=names)
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscription) -> None:
        self._subscribers.discard(subscriber)

    @contextlib.contextmanager
    def subscription(
        self,
        *,
        maxsize: int = DEFAULT_QUEUE_SIZE,
        names: Iterable[str] | None = None,
    ) -> Iterator[Subscription]:
        """Subscribe for the duration of a block.

        The only registration path the WebSocket endpoint uses: a connection
        that fails anywhere between accept and close must not leave a queue
        behind that the bus keeps filling forever.
        """
        subscriber = self.subscribe(maxsize=maxsize, names=names)
        try:
            yield subscriber
        finally:
            self.unsubscribe(subscriber)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # --- Catching up -----------------------------------------------------

    def replay_since(self, seq: int) -> list[Event] | None:
        """Events after ``seq``, or ``None`` when the gap cannot be filled.

        ``None`` is the important answer: it means the client missed more than
        the buffer holds (or is quoting a sequence from before a restart) and
        has to refetch state rather than assume it is up to date.
        """
        if seq < 0 or seq > self._sequence:
            return None
        if not self._history:
            # Nothing has been published, so being at 0 is being current.
            return [] if seq == self._sequence else None
        if seq < self._history[0].seq - 1:
            return None
        return [event for event in self._history if event.seq > seq]

    def reset(self) -> None:
        """Test helper: forget subscribers, history and the counter."""
        self._sequence = 0
        self._subscribers.clear()
        self._history.clear()


_bus = EventBus()


def get_bus() -> EventBus:
    return _bus


def publish(event_name: str, payload: Mapping[str, Any] | None = None) -> Event | None:
    """Publish on the process bus. The entry point every subsystem uses."""
    return _bus.publish(event_name, payload)


def reset_bus() -> None:
    """Test helper: return the process bus to its start-up state."""
    _bus.reset()
