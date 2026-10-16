"""The in-process event bus: publishing, subscribing, replay and redaction."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.events.bus import (
    DEFAULT_HISTORY_SIZE,
    EVENT_NAMES,
    MOTION_STARTED,
    RECORDING_STARTED,
    Event,
    EventBus,
    Subscription,
)


class TestPublishing:
    def test_sequence_numbers_start_at_one_and_increase(self) -> None:
        bus = EventBus()
        first = bus.publish(MOTION_STARTED, {"camera_id": "a"})
        second = bus.publish(MOTION_STARTED, {"camera_id": "a"})
        assert first is not None
        assert second is not None
        assert (first.seq, second.seq) == (1, 2)
        assert bus.sequence == 2

    def test_an_unknown_event_name_is_rejected_not_raised(self) -> None:
        bus = EventBus()
        result = bus.publish("nonsense.event", {})
        assert result is None
        assert bus.sequence == 0

    def test_every_documented_name_is_actually_publishable(self) -> None:
        bus = EventBus()
        for name in EVENT_NAMES:
            assert bus.publish(name, {}) is not None

    def test_publish_never_raises_even_with_a_hostile_payload(self) -> None:
        bus = EventBus()

        class Cyclic:
            def __init__(self) -> None:
                self.ref: Cyclic | None = self

        # Deeply/self nested structures must be swallowed by the depth guard,
        # not turned into an exception that would take down a motion worker.
        assert bus.publish(MOTION_STARTED, {"self": Cyclic()}) is not None

    def test_no_payload_is_the_same_as_an_empty_one(self) -> None:
        bus = EventBus()
        event = bus.publish(MOTION_STARTED)
        assert event is not None
        assert event.data == {}

    def test_to_message_is_fully_json_native(self) -> None:
        bus = EventBus()
        event = bus.publish(RECORDING_STARTED, {"camera_id": "cam-1"})
        assert event is not None
        message = event.to_message()
        assert message["type"] == RECORDING_STARTED
        assert message["seq"] == 1
        assert message["ts"].endswith("Z")
        assert message["data"] == {"camera_id": "cam-1"}


class TestRedaction:
    def test_sensitive_keys_are_dropped_not_masked(self) -> None:
        bus = EventBus()
        event = bus.publish(MOTION_STARTED, {"password": "hunter2", "camera_id": "a"})
        assert event is not None
        assert "password" not in event.data
        assert event.data["camera_id"] == "a"

    def test_credentials_embedded_in_a_url_are_redacted(self) -> None:
        bus = EventBus()
        event = bus.publish(MOTION_STARTED, {"url": "rtsp://admin:hunter2@192.168.50.5:554/stream"})
        assert event is not None
        assert "hunter2" not in event.data["url"]
        assert "admin" not in event.data["url"]

    def test_non_json_native_values_become_strings_not_exceptions(self) -> None:
        bus = EventBus()

        class Odd:
            def __str__(self) -> str:
                return "odd-value"

        event = bus.publish(MOTION_STARTED, {"thing": Odd()})
        assert event is not None
        assert event.data["thing"] == "odd-value"

    def test_nested_structures_are_cleaned_recursively(self) -> None:
        bus = EventBus()
        event = bus.publish(
            MOTION_STARTED,
            {"nested": {"password": "x", "list": [{"token": "y", "keep": "z"}]}},
        )
        assert event is not None
        assert "password" not in event.data["nested"]
        assert "token" not in event.data["nested"]["list"][0]
        assert event.data["nested"]["list"][0]["keep"] == "z"


class TestSubscribing:
    async def test_a_subscriber_receives_a_published_event(self) -> None:
        bus = EventBus()
        with bus.subscription() as sub:
            bus.publish(MOTION_STARTED, {"camera_id": "a"})
            event = await asyncio.wait_for(sub.get(), timeout=1)
        assert event.name == MOTION_STARTED

    async def test_a_subscriber_only_gets_the_names_it_asked_for(self) -> None:
        bus = EventBus()
        with bus.subscription(names={MOTION_STARTED}) as sub:
            bus.publish(RECORDING_STARTED, {})
            bus.publish(MOTION_STARTED, {"camera_id": "a"})
            event = await asyncio.wait_for(sub.get(), timeout=1)
            assert event.name == MOTION_STARTED
            assert sub.pending == 0

    def test_asking_for_no_known_names_means_nothing_arrives(self) -> None:
        bus = EventBus()
        with bus.subscription(names=set()) as sub:
            bus.publish(MOTION_STARTED, {})
            assert sub.pending == 0

    def test_unsubscribing_stops_delivery(self) -> None:
        bus = EventBus()
        sub = bus.subscribe()
        assert bus.subscriber_count == 1
        bus.unsubscribe(sub)
        assert bus.subscriber_count == 0
        bus.publish(MOTION_STARTED, {})
        assert sub.pending == 0

    def test_the_subscription_context_manager_unsubscribes_even_on_error(self) -> None:
        bus = EventBus()
        try:
            with bus.subscription():
                assert bus.subscriber_count == 1
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert bus.subscriber_count == 0

    def test_a_full_queue_drops_the_oldest_event_not_the_newest(self) -> None:
        bus = EventBus()
        with bus.subscription(maxsize=2) as sub:
            bus.publish(MOTION_STARTED, {"n": 1})
            bus.publish(MOTION_STARTED, {"n": 2})
            bus.publish(MOTION_STARTED, {"n": 3})
            assert sub.dropped == 1
            assert sub.pending == 2

    async def test_a_slow_subscriber_never_blocks_publish(self) -> None:
        """Publishing is synchronous and must return immediately regardless of
        how full a subscriber's queue already is -- a stalled browser tab must
        never be able to stall a motion worker calling publish()."""
        bus = EventBus()
        with bus.subscription(maxsize=1):
            for n in range(50):
                event = bus.publish(MOTION_STARTED, {"n": n})
                assert event is not None
        assert bus.sequence == 50


class TestReplay:
    def test_replaying_from_the_current_sequence_yields_nothing(self) -> None:
        bus = EventBus()
        bus.publish(MOTION_STARTED, {})
        assert bus.replay_since(bus.sequence) == []

    def test_replaying_from_zero_on_a_fresh_bus_is_an_empty_catch_up(self) -> None:
        bus = EventBus()
        assert bus.replay_since(0) == []

    def test_replaying_from_before_the_first_event_returns_everything(self) -> None:
        bus = EventBus()
        bus.publish(MOTION_STARTED, {"n": 1})
        bus.publish(MOTION_STARTED, {"n": 2})
        replayed = bus.replay_since(0)
        assert replayed is not None
        assert [event.data["n"] for event in replayed] == [1, 2]

    def test_replaying_from_the_middle_returns_only_what_is_newer(self) -> None:
        bus = EventBus()
        bus.publish(MOTION_STARTED, {"n": 1})
        bus.publish(MOTION_STARTED, {"n": 2})
        bus.publish(MOTION_STARTED, {"n": 3})
        replayed = bus.replay_since(2)
        assert replayed is not None
        assert [event.data["n"] for event in replayed] == [3]

    def test_a_gap_larger_than_the_history_buffer_cannot_be_filled(self) -> None:
        bus = EventBus(history_size=3)
        for n in range(10):
            bus.publish(MOTION_STARTED, {"n": n})
        # Only the last 3 are still buffered; asking for more than that back
        # must be told "no", not handed a history with a silent hole in it.
        assert bus.replay_since(0) is None

    def test_a_bus_built_with_no_arguments_uses_the_documented_default_size(self) -> None:
        bus = EventBus()
        for n in range(DEFAULT_HISTORY_SIZE + 10):
            bus.publish(MOTION_STARTED, {"n": n})
        # The oldest 10 have already fallen out of the default-sized buffer.
        assert bus.replay_since(9) is None
        assert bus.replay_since(10) is not None

    def test_a_negative_sequence_is_rejected(self) -> None:
        bus = EventBus()
        bus.publish(MOTION_STARTED, {})
        assert bus.replay_since(-1) is None

    def test_a_sequence_from_the_future_is_rejected(self) -> None:
        bus = EventBus()
        bus.publish(MOTION_STARTED, {})
        assert bus.replay_since(999) is None

    def test_history_respects_its_configured_size(self) -> None:
        bus = EventBus(history_size=5)
        for n in range(20):
            bus.publish(MOTION_STARTED, {"n": n})
        # Seq 20 published; the last 5 (16-20) are still buffered. Asking from
        # exactly one before the oldest kept event is the last seq that can
        # still be filled without a gap.
        replayed = bus.replay_since(15)
        assert replayed is not None
        assert len(replayed) == 5
        assert [event.data["n"] for event in replayed] == [15, 16, 17, 18, 19]


class TestModuleLevelBus:
    def test_reset_bus_clears_the_process_wide_singleton(self) -> None:
        from app.events.bus import get_bus, publish, reset_bus

        publish(MOTION_STARTED, {})
        assert get_bus().sequence >= 1
        reset_bus()
        assert get_bus().sequence == 0
        assert get_bus().subscriber_count == 0


class TestEventDataclass:
    def test_event_is_immutable(self) -> None:
        event = Event(seq=1, name=MOTION_STARTED, ts=datetime.now(UTC))
        # frozen=True: assigning after construction must raise, not silently
        # allow a subscriber to mutate an event another subscriber already has.
        try:
            event.seq = 2  # type: ignore[misc]
        except AttributeError:
            pass
        else:
            raise AssertionError("Event must be frozen")


class TestSubscriptionWants:
    def test_wants_everything_when_no_filter_was_given(self) -> None:
        sub = Subscription()
        event = Event(seq=1, name=MOTION_STARTED, ts=datetime.now(UTC))
        assert sub.wants(event) is True
