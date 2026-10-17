"""``WS /api/v1/events/stream`` -- the realtime push endpoint.

Full wire protocol in ``app/integrations/CONTRACT.md`` section 5. The short
version:

* Authentication happens before the socket is accepted -- an unauthorised
  caller gets WebSocket code 1008 and never receives a single frame, the
  same guarantee ``app.streaming.router``'s live-view socket makes.
* ``?since=<seq>`` resumes from the bus's small ring buffer
  (:meth:`app.events.bus.EventBus.replay_since`); a gap too large to fill is
  answered with a ``resync_required`` frame instead of pretending to be
  caught up.
* A heartbeat flows both ways: the server sends one on an otherwise-idle
  connection, and answers a client-sent ``{"type": "ping"}`` with
  ``{"type": "pong"}`` immediately. A connection that sends nothing at all
  for a long time is closed to free the slot.
* Publishing is handled entirely by :mod:`app.events.bus` -- this endpoint
  only ever drains its own bounded subscription queue, so a browser tab on a
  suspended laptop can never slow down whatever just published an event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.auth.deps import get_optional_principal
from app.config import get_settings
from app.database.session import get_session_factory
from app.events.bus import Event, EventBus, Subscription, get_bus
from app.logging_config import get_logger
from app.security.origin import websocket_origin_allowed

router = APIRouter(prefix="/events", tags=["events"])
logger = get_logger("API")

#: Server -> client keepalive when nothing else has been sent. Comfortably
#: under most reverse-proxy and NAT idle-connection timeouts.
HEARTBEAT_INTERVAL_SECONDS: Final = 20.0

#: A connection that has sent nothing at all -- no ping, no pong, not even a
#: protocol-level frame -- in this long is presumed abandoned and closed.
#: Several missed heartbeat intervals of slack, so one dropped round trip
#: never costs the connection.
CLIENT_IDLE_TIMEOUT_SECONDS: Final = 75.0


class _Activity:
    """The one thing the reader and writer tasks share: when the client was
    last heard from, for the idle-timeout check."""

    __slots__ = ("last_seen",)

    def __init__(self) -> None:
        self.last_seen = time.monotonic()

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def idle_for(self) -> float:
        return time.monotonic() - self.last_seen


def _now_iso() -> str:
    """Matches :meth:`Event.to_message`'s own timestamp formatting exactly,
    so every frame on this socket looks the same to a client."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_since(raw: str | None) -> int | None:
    """A missing or unparsable ``since`` behaves like a fresh connection
    rather than an error -- a client that sends garbage still gets a
    working stream, just without resuming."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _bootstrap_messages(bus: EventBus, since: int | None) -> tuple[list[dict[str, Any]], int]:
    """The frames to send immediately after accepting, and the sequence
    number the live loop should start delivering strictly after.

    Deliberately a pure function of ``bus`` and ``since`` -- no socket, no
    asyncio -- so the three cases (fresh connect, successful resume,
    resync-required) are each one direct test.
    """
    if since is None:
        return [{"type": "hello", "resumed": False, "replayed": 0}], bus.sequence

    replay: list[Event] | None = bus.replay_since(since)
    if replay is None:
        # Deliberately carries `seq`, unlike `hello`/`heartbeat` -- see
        # CONTRACT.md section 5 for why: it lets a client that only checks
        # "is this seq exactly one more than the last" notice the jump and
        # resynchronise on its own, with no special case for the frame type.
        return [{"type": "resync_required", "seq": bus.sequence}], bus.sequence

    messages: list[dict[str, Any]] = [{"type": "hello", "resumed": True, "replayed": len(replay)}]
    messages.extend(event.to_message() for event in replay)
    last_seq = replay[-1].seq if replay else since
    return messages, last_seq


async def _read_client(websocket: WebSocket, send_lock: asyncio.Lock, activity: _Activity) -> None:
    """Notice a disconnect, and answer an application-level ping.

    The only task that ever calls ``receive()`` on this socket. Sending the
    pong reply through ``send_lock`` is what makes it safe for this task to
    write occasionally while :func:`_write_events` is the socket's regular
    writer -- two concurrent, uncoordinated ``send`` calls on one WebSocket
    is not something to risk.
    """
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        activity.touch()

        text = message.get("text")
        if text is None:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "ping":
            async with send_lock:
                await websocket.send_json({"type": "pong"})


async def _write_events(
    websocket: WebSocket,
    bus: EventBus,
    subscription: Subscription,
    send_lock: asyncio.Lock,
    activity: _Activity,
    last_sent_seq: int,
) -> None:
    """Deliver bus events as they arrive, and a heartbeat when none do.

    ``last_sent_seq`` starts wherever :func:`_bootstrap_messages` left off,
    and every event already queued on ``subscription`` before that point is
    skipped here rather than resent -- the subscription is opened before the
    replay is computed (see the handler below), so the two can legitimately
    overlap.
    """
    while True:
        try:
            event = await asyncio.wait_for(subscription.get(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            if activity.idle_for() > CLIENT_IDLE_TIMEOUT_SECONDS:
                async with send_lock:
                    with contextlib.suppress(RuntimeError):
                        await websocket.close(
                            code=status.WS_1000_NORMAL_CLOSURE, reason="Idle timeout"
                        )
                return
            async with send_lock:
                await websocket.send_json(
                    {"type": "heartbeat", "server_seq": bus.sequence, "ts": _now_iso()}
                )
            continue

        if event.seq <= last_sent_seq:
            continue
        last_sent_seq = event.seq
        async with send_lock:
            await websocket.send_json(event.to_message())


@router.websocket("/stream")
async def event_stream(websocket: WebSocket) -> None:
    settings = get_settings()
    if not websocket_origin_allowed(websocket, settings):
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Cross-site socket refused"
        )
        return

    # Resolved by hand rather than through FastAPI's Depends, exactly like
    # app.streaming.router's live-view socket: authentication must finish
    # before the socket is accepted, so a rejected caller never receives a
    # frame, not even a close frame with a body.
    async with get_session_factory()() as session:
        principal = await get_optional_principal(websocket, session, settings)
    if principal is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorised")
        return

    bus = get_bus()
    since = _parse_since(websocket.query_params.get("since"))

    # Subscribed before the replay is computed: an event published in the
    # gap between the two is then merely delivered twice as far as the queue
    # is concerned, and _write_events's `last_sent_seq` check drops the
    # duplicate. Computing the replay first could instead lose it outright.
    with bus.subscription() as subscription:
        await websocket.accept()
        logger.info(
            "Realtime client connected",
            extra={"principal": principal.name, "kind": principal.kind, "since": since},
        )

        bootstrap, last_sent_seq = _bootstrap_messages(bus, since)
        send_lock = asyncio.Lock()
        try:
            for message in bootstrap:
                await websocket.send_json(message)
        except (RuntimeError, WebSocketDisconnect):
            return

        activity = _Activity()
        tasks = [
            asyncio.create_task(
                _read_client(websocket, send_lock, activity),
                name=f"realtime-read:{principal.id}",
            ),
            asyncio.create_task(
                _write_events(websocket, bus, subscription, send_lock, activity, last_sent_seq),
                name=f"realtime-write:{principal.id}",
            ),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
