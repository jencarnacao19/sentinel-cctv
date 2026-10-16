"""The realtime WebSocket endpoint, over a real ASGI/TCP transport.

``httpx`` has no WebSocket support, and wrapping the already-lifespan-started
``app`` fixture in ``starlette.testclient.TestClient`` would run the
application's lifespan a second time on the same instance. Instead this binds
a real ``uvicorn`` server to an ephemeral loopback port with its own lifespan
turned off (the ``app`` fixture already ran it) and drives the socket with
the ``websockets`` client library Sentinel already depends on -- real TCP,
real HTTP upgrade, real ASGI routing, exactly what a browser or Home
Assistant would do.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import uvicorn
import websockets
from fastapi import FastAPI
from httpx import AsyncClient
from websockets.exceptions import InvalidStatus

from app import realtime
from app.config import Settings
from app.events.bus import MOTION_STARTED, RECORDING_STOPPED, get_bus, reset_bus


@pytest.fixture(autouse=True)
def _isolated_bus() -> AsyncIterator[None]:
    """Every test in this file gets a bus with no history and no subscribers.

    The bus is a process-wide singleton (`app.events.bus.get_bus`), so
    without this, sequence numbers and subscriber counts would leak between
    tests -- and, since pytest runs every test file in one process, into
    whatever runs after this file too.
    """
    reset_bus()
    yield
    reset_bus()


@pytest.fixture
async def ws_server(app: FastAPI) -> AsyncIterator[str]:
    """A real TCP server serving ``app``, sharing the lifespan the ``app``
    fixture already started -- ``lifespan="off"`` is what stops uvicorn from
    trying to run it a second time.

    ``app.api.v1`` does not mount ``app.realtime.router`` yet -- that wiring
    is the orchestrator's job once this package is integrated (see this
    package's report), not something this phase may do itself since
    ``backend/app/api/v1.py`` is off limits here. Tests exercise the real
    router by mounting it onto this already-lifespan-started app directly,
    rather than duplicating everything ``create_app()`` sets up.
    """
    from app.realtime.router import router as realtime_router

    if not any(getattr(route, "name", None) == "event_stream" for route in app.routes):
        app.include_router(realtime_router, prefix="/api/v1")

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        lifespan="off",
        # A default log_config replaces the root logger's handlers wholesale,
        # which would strip Sentinel's redaction filter for the rest of the
        # test session. None skips that entirely; conftest's configure_logging
        # already put the handlers Sentinel wants in place.
        log_config=None,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def _ws_url(base: str, *, query: str = "") -> str:
    return f"ws://{base}/api/v1/events/stream{query}"


class TestAuthentication:
    @pytest.mark.parametrize("scope", ["read_only", "camera_control"])
    async def test_scoped_ha_tokens_use_bearer_websocket_auth_and_can_be_revoked(
        self, ws_server: str, admin_client: AsyncClient, scope: str
    ) -> None:
        created = await admin_client.post(
            "/api/v1/auth/tokens", json={"name": "HA", "scope": scope}
        )
        assert created.status_code == 201
        token = created.json()
        headers = {"Authorization": f"Bearer {token['token']}"}
        async with websockets.connect(_ws_url(ws_server), additional_headers=headers) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello["type"] == "hello"
        revoked = await admin_client.delete(f"/api/v1/auth/tokens/{token['id']}")
        assert revoked.status_code == 204
        with pytest.raises(InvalidStatus) as exc_info:
            async with websockets.connect(_ws_url(ws_server), additional_headers=headers):
                pass
        assert exc_info.value.response.status_code == 403

    async def test_an_unauthenticated_connection_never_reaches_the_application(
        self, ws_server: str
    ) -> None:
        """Rejected before `accept()`, which uvicorn's ASGI websocket
        implementation turns into a plain HTTP 403 -- not a completed
        upgrade followed by a close frame. No hello, no anything."""
        with pytest.raises(InvalidStatus) as exc_info:
            async with websockets.connect(_ws_url(ws_server)):
                pass
        assert exc_info.value.response.status_code == 403

    async def test_an_invalid_bearer_token_is_refused(self, ws_server: str) -> None:
        with pytest.raises(InvalidStatus) as exc_info:
            async with websockets.connect(
                _ws_url(ws_server), additional_headers={"Authorization": "Bearer sctv_nonsense"}
            ):
                pass
        assert exc_info.value.response.status_code == 403

    async def test_a_bearer_header_authenticates_the_socket(
        self, ws_server: str, admin_client: AsyncClient
    ) -> None:
        """The header path Home Assistant's own client uses."""
        async with websockets.connect(
            _ws_url(ws_server),
            additional_headers={"Authorization": admin_client.headers["Authorization"]},
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello == {"type": "hello", "resumed": False, "replayed": 0}

    async def test_a_session_cookie_authenticates_the_socket(
        self, ws_server: str, admin_client: AsyncClient, settings: Settings
    ) -> None:
        """The path the browser UI uses -- no Authorization header at all."""
        cookie_value = admin_client.cookies.get(settings.session_cookie_name)
        assert cookie_value
        async with websockets.connect(
            _ws_url(ws_server),
            additional_headers={"Cookie": f"{settings.session_cookie_name}={cookie_value}"},
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello["type"] == "hello"

    async def test_a_query_string_token_authenticates_the_socket(
        self, ws_server: str, admin_client: AsyncClient
    ) -> None:
        """The fallback for a client that cannot set a header on the upgrade
        request at all -- documented in CONTRACT.md as available, even though
        Home Assistant's own client prefers the header."""
        created = await admin_client.post("/api/v1/auth/tokens", json={"name": "ha"})
        assert created.status_code == 201, created.text
        token = created.json()["token"]

        async with websockets.connect(_ws_url(ws_server, query=f"?token={token}")) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello["type"] == "hello"


class TestDeliveryAndResume:
    async def test_a_published_event_is_delivered_live(
        self, ws_server: str, admin_client: AsyncClient
    ) -> None:
        async with websockets.connect(
            _ws_url(ws_server),
            additional_headers={"Authorization": admin_client.headers["Authorization"]},
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # hello

            published = get_bus().publish(MOTION_STARTED, {"camera_id": "cam-1"})
            assert published is not None

            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["type"] == MOTION_STARTED
            assert frame["seq"] == published.seq
            assert frame["data"] == {"camera_id": "cam-1"}
            assert frame["ts"].endswith("Z")

    async def test_a_fresh_connection_replays_nothing(
        self, ws_server: str, admin_client: AsyncClient
    ) -> None:
        get_bus().publish(MOTION_STARTED, {"n": 1})
        async with websockets.connect(
            _ws_url(ws_server),
            additional_headers={"Authorization": admin_client.headers["Authorization"]},
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello == {"type": "hello", "resumed": False, "replayed": 0}

    async def test_since_resumes_with_exactly_the_missed_events(
        self, ws_server: str, admin_client: AsyncClient
    ) -> None:
        bus = get_bus()
        first = bus.publish(MOTION_STARTED, {"n": 1})
        second = bus.publish(MOTION_STARTED, {"n": 2})
        third = bus.publish(RECORDING_STOPPED, {"n": 3})
        assert first and second and third

        async with websockets.connect(
            _ws_url(ws_server, query=f"?since={first.seq}"),
            additional_headers={"Authorization": admin_client.headers["Authorization"]},
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello == {"type": "hello", "resumed": True, "replayed": 2}

            replayed = [json.loads(await asyncio.wait_for(ws.recv(), timeout=5)) for _ in range(2)]
            assert [frame["seq"] for frame in replayed] == [second.seq, third.seq]
            assert [frame["type"] for frame in replayed] == [MOTION_STARTED, RECORDING_STOPPED]

            # A live event afterwards is not re-delivered from the replay.
            live = bus.publish(MOTION_STARTED, {"n": 4})
            assert live is not None
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["seq"] == live.seq

    async def test_a_gap_too_large_to_fill_is_told_to_resynchronise(
        self, ws_server: str, admin_client: AsyncClient
    ) -> None:
        bus = get_bus()
        # Comfortably past the bus's history buffer (256), so `since=1` can no
        # longer be filled from it.
        for n in range(300):
            bus.publish(MOTION_STARTED, {"n": n})

        async with websockets.connect(
            _ws_url(ws_server, query="?since=1"),
            additional_headers={"Authorization": admin_client.headers["Authorization"]},
        ) as ws:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["type"] == "resync_required"
            assert frame["seq"] == bus.sequence

            # The connection stays open and keeps delivering live events --
            # resync_required is not a reason to disconnect the client.
            live = bus.publish(MOTION_STARTED, {"n": "after-resync"})
            assert live is not None
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["seq"] == live.seq

    async def test_garbage_since_behaves_like_a_fresh_connection(
        self, ws_server: str, admin_client: AsyncClient
    ) -> None:
        async with websockets.connect(
            _ws_url(ws_server, query="?since=not-a-number"),
            additional_headers={"Authorization": admin_client.headers["Authorization"]},
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello == {"type": "hello", "resumed": False, "replayed": 0}


class TestApplicationLevelPing:
    async def test_a_client_ping_gets_a_pong(
        self, ws_server: str, admin_client: AsyncClient
    ) -> None:
        async with websockets.connect(
            _ws_url(ws_server),
            additional_headers={"Authorization": admin_client.headers["Authorization"]},
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # hello
            await ws.send(json.dumps({"type": "ping"}))
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame == {"type": "pong"}

    async def test_an_unparsable_client_frame_is_ignored_not_fatal(
        self, ws_server: str, admin_client: AsyncClient
    ) -> None:
        async with websockets.connect(
            _ws_url(ws_server),
            additional_headers={"Authorization": admin_client.headers["Authorization"]},
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # hello
            await ws.send("not json at all")

            # The connection must still be alive and useful afterwards.
            published = get_bus().publish(MOTION_STARTED, {"still": "here"})
            assert published is not None
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["seq"] == published.seq


class TestHeartbeatAndIdleTimeout:
    async def test_a_heartbeat_arrives_on_an_idle_connection_and_a_still_idle_one_is_closed(
        self, ws_server: str, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Idle timeout just over one heartbeat interval: exactly one heartbeat
        # fires (at ~0.2s), then the connection is still idle at the next
        # timeout check (~0.4s, past the 0.3s threshold) and gets closed.
        monkeypatch.setattr(realtime.router, "HEARTBEAT_INTERVAL_SECONDS", 0.2)
        monkeypatch.setattr(realtime.router, "CLIENT_IDLE_TIMEOUT_SECONDS", 0.3)

        async with websockets.connect(
            _ws_url(ws_server),
            additional_headers={"Authorization": admin_client.headers["Authorization"]},
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # hello

            heartbeat = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert heartbeat["type"] == "heartbeat"
            assert "server_seq" in heartbeat
            assert heartbeat["ts"].endswith("Z")

            # Still nothing sent by the client: the next thing to arrive is
            # the server closing the connection, not another heartbeat.
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=3)
            assert ws.close_code == 1000

    async def test_a_client_that_keeps_pinging_is_never_dropped_as_idle(
        self, ws_server: str, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(realtime.router, "HEARTBEAT_INTERVAL_SECONDS", 0.15)
        monkeypatch.setattr(realtime.router, "CLIENT_IDLE_TIMEOUT_SECONDS", 0.35)

        async with websockets.connect(
            _ws_url(ws_server),
            additional_headers={"Authorization": admin_client.headers["Authorization"]},
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # hello

            # Five round trips at 0.1s apart span half a second -- comfortably
            # longer than the 0.35s idle timeout above -- while never leaving
            # the connection idle for longer than that timeout at a time.
            for _ in range(5):
                await ws.send(json.dumps({"type": "ping"}))
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                # A heartbeat can legitimately win the race with the pong if
                # the server's own timer fires first; either one proves the
                # same thing -- the connection is still open and answering.
                assert frame["type"] in ("pong", "heartbeat")
                await asyncio.sleep(0.1)

            # One more round trip, well past the point an idle connection
            # would have been closed, proves it really is still alive rather
            # than having survived by luck on the last iteration.
            await ws.send(json.dumps({"type": "ping"}))
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert frame["type"] in ("pong", "heartbeat")
