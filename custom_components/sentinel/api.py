"""HTTP and WebSocket client for a Sentinel CCTV server.

Imports aiohttp but never Home Assistant, so the whole transport layer -- URL
handling, error mapping, the reconnect loop -- can be exercised against a real
local aiohttp server in unit tests.

The split of responsibilities is the point of the integration: REST is used once
at start-up and then only for snapshots, commands and a slow safety-net refresh;
everything that happens in real time arrives on the WebSocket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse, urlunparse

from aiohttp import ClientError, ClientSession, ClientWebSocketResponse, WSMsgType

from .const import (
    API_PREFIX,
    DEFAULT_WEBSOCKET_PATH,
    HANDSHAKE_PATH,
    RECONNECT_BASE_SECONDS,
    RECONNECT_MAX_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    SNAPSHOT_TIMEOUT_SECONDS,
    SUPPORTED_API_VERSION,
    WEBSOCKET_APPLICATION_PING_SECONDS,
    WEBSOCKET_HEARTBEAT_SECONDS,
)
from .models import CameraInfo, SentinelEvent, ServerInfo, parse_cameras

_LOGGER = logging.getLogger(__name__)

#: Beyond this the exponent is pointless: the delay is already at its cap, and
#: 2 ** attempt on a process that has been retrying for weeks would overflow.
_MAX_BACKOFF_EXPONENT = 16

#: A snapshot arrives as image/jpeg. Some reverse proxies rewrite that to a
#: generic binary type, which is accepted; an HTML error page is not.
_IMAGE_CONTENT_TYPES = ("image/", "application/octet-stream")


class SentinelError(Exception):
    """Base class for every failure raised by this client."""


class SentinelConnectionError(SentinelError):
    """The server could not be reached at all."""


class SentinelAuthError(SentinelError):
    """The server rejected the API token."""


class SentinelApiError(SentinelError):
    """The server answered, but not with what was asked for."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SentinelUnsupportedVersionError(SentinelError):
    """The server cannot speak the Home Assistant integration protocol.

    Raised both when the handshake endpoint does not exist at all (a Sentinel
    build from before this integration existed) and when it exists but reports
    an API version this client does not understand.
    """


def normalise_base_url(raw: str) -> str:
    """Turn whatever the user typed into a base URL this client can use.

    People paste the address bar of the Sentinel UI, the API root, or a bare
    ``host:port``. All three have to end up as the same origin. Any userinfo is
    dropped: Sentinel authenticates with a bearer token, and credentials in a URL
    would leak into logs and diagnostics for no benefit.

    Raises:
        ValueError: the value is not an http(s) address with a host.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("The server address is empty")
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme: {parsed.scheme}")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as err:  # malformed port
        raise ValueError("The server address has an invalid port") from err
    if not host:
        raise ValueError("The server address has no host")

    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = parsed.path.rstrip("/")
    # A pasted API root still identifies the same server; keep any other prefix,
    # which is how Sentinel is reached behind a reverse proxy.
    for suffix in (API_PREFIX, "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break

    return urlunparse((parsed.scheme, netloc, path, "", "", ""))


def websocket_url(
    base_url: str, path: str = DEFAULT_WEBSOCKET_PATH, *, since: int | None = None
) -> str:
    """Absolute ws(s) URL for a server-reported WebSocket path.

    ``since`` asks the server to replay events after that sequence number on
    this same connection, which is how a reconnect resumes instead of silently
    skipping whatever happened while the socket was down.
    """
    if path.startswith(("ws://", "wss://")):
        url = path
    elif path.startswith(("http://", "https://")):
        url = "ws" + path[4:]
    else:
        base = base_url.rstrip("/")
        scheme, _, remainder = base.partition("://")
        ws_scheme = "wss" if scheme == "https" else "ws"
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{ws_scheme}://{remainder}{path}"
    if since is not None:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}since={since}"
    return url


def reconnect_delay(
    attempt: int,
    *,
    base: float = RECONNECT_BASE_SECONDS,
    maximum: float = RECONNECT_MAX_SECONDS,
) -> float:
    """Seconds to wait before reconnect number ``attempt`` (0 is the first retry)."""
    exponent = min(max(attempt, 0), _MAX_BACKOFF_EXPONENT)
    return min(base * (2**exponent), maximum)


class SentinelClient:
    """Talks to one Sentinel server."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        token: str,
        *,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._session = session
        self.base_url = normalise_base_url(base_url)
        self._token = token
        self._request_timeout = request_timeout

    @property
    def token(self) -> str:
        return self._token

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.base_url}{API_PREFIX}{path}"

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        expect_image: bool = False,
    ) -> Any:
        url = self._url(path)
        try:
            async with asyncio.timeout(timeout or self._request_timeout):
                async with self._session.request(
                    method, url, headers=self._auth_headers, json=json
                ) as response:
                    if response.status in (401, 403):
                        raise SentinelAuthError("The Sentinel API token was rejected")
                    if response.status >= 400:
                        raise SentinelApiError(
                            f"Sentinel returned HTTP {response.status} for {path}",
                            status=response.status,
                        )
                    if expect_image:
                        content_type = (response.content_type or "").lower()
                        if not content_type.startswith(_IMAGE_CONTENT_TYPES):
                            raise SentinelApiError(
                                f"Expected an image from {path}, got {content_type or 'nothing'}",
                                status=response.status,
                            )
                        return await response.read()
                    # Home Assistant's aiohttp test response intentionally
                    # exposes only the public request surface, and older
                    # harness releases have no ``content_length`` attribute.
                    # A missing/unknown length is equivalent to a chunked
                    # response here: read it and decide from the actual body.
                    if response.status == 204 or not getattr(
                        response, "content_length", None
                    ):
                        # A command endpoint may answer with no body at all.
                        body = await response.read()
                        if not body:
                            return {}
                    # content_type=None: trust the endpoint, not the header, which
                    # a proxy may have rewritten.
                    return await response.json(content_type=None)
        except TimeoutError as err:
            raise SentinelConnectionError(f"Sentinel timed out on {path}") from err
        except ClientError as err:
            raise SentinelConnectionError(
                f"Cannot reach Sentinel at {self.base_url}"
            ) from err
        except ValueError as err:  # malformed JSON body
            raise SentinelApiError(
                f"Sentinel sent an unreadable response for {path}"
            ) from err

    async def async_get_server_info(self) -> ServerInfo:
        """Handshake used by the config flow and by every start-up.

        Doubles as credential validation: it is the cheapest authenticated call.
        """
        try:
            payload = await self._async_request("GET", HANDSHAKE_PATH)
        except SentinelApiError as err:
            if err.status == 404:
                # Not "cannot connect" -- the server answered fine, it just has
                # never heard of this endpoint. That is a version problem, and
                # saying so saves a confused trip through firewall settings.
                raise SentinelUnsupportedVersionError(
                    "This Sentinel server has no Home Assistant integration endpoint. "
                    "Update Sentinel to a version that supports Home Assistant."
                ) from err
            raise
        if not isinstance(payload, dict):
            raise SentinelApiError("The integration handshake did not return an object")
        try:
            info = ServerInfo.from_payload(payload)
        except ValueError as err:
            raise SentinelApiError(str(err)) from err
        if info.api_version is not None and info.api_version != SUPPORTED_API_VERSION:
            raise SentinelUnsupportedVersionError(
                f"This Sentinel server speaks Home Assistant integration API version "
                f"{info.api_version}, but this integration supports version "
                f"{SUPPORTED_API_VERSION}. Update Sentinel or this integration so both "
                f"sides match."
            )
        return info

    async def async_get_cameras(self) -> tuple[CameraInfo, ...]:
        payload = await self._async_request("GET", "/cameras")
        return parse_cameras(payload)

    async def async_get_snapshot(self, camera_id: str) -> bytes:
        image = await self._async_request(
            "GET",
            f"/cameras/{camera_id}/snapshot",
            timeout=SNAPSHOT_TIMEOUT_SECONDS,
            expect_image=True,
        )
        return bytes(image)

    async def async_test_camera(self, camera_id: str) -> dict[str, Any]:
        payload = await self._async_request(
            "POST", f"/cameras/{camera_id}/test", timeout=SNAPSHOT_TIMEOUT_SECONDS
        )
        return payload if isinstance(payload, dict) else {}

    async def async_get_health(self) -> dict[str, Any]:
        payload = await self._async_request("GET", "/system/health")
        return payload if isinstance(payload, dict) else {}

    async def async_get_system_info(self) -> dict[str, Any]:
        payload = await self._async_request("GET", "/system")
        return payload if isinstance(payload, dict) else {}

    async def async_get_storage(self) -> dict[str, Any]:
        payload = await self._async_request("GET", "/storage")
        return payload if isinstance(payload, dict) else {}

    async def async_get_retention_days(self) -> int | None:
        payload = await self._async_request("GET", "/storage/settings")
        if not isinstance(payload, dict):
            return None
        value = payload.get("retention_days")
        return value if isinstance(value, int) else None

    async def async_get_event_count(self, camera_id: str) -> int | None:
        """Lifetime motion-event count for one camera.

        There is no dedicated count endpoint, so this asks for the smallest
        page the list endpoint allows and reads its ``total`` -- one small
        request per camera per refresh rather than a full event list.
        """
        payload = await self._async_request(
            "GET", f"/events?camera_id={camera_id}&limit=1&offset=0"
        )
        if not isinstance(payload, dict):
            return None
        total = payload.get("total")
        return total if isinstance(total, int) else None

    async def async_set_recording_mode(self, camera_id: str, mode: str) -> None:
        """Change what a camera records.

        The switch entity is the only writer of this field; everything else
        that touches recording mode goes through Sentinel's own interface, so
        this is deliberately narrow rather than a general camera-update call.
        """
        await self._async_request(
            "PATCH", f"/cameras/{camera_id}", json={"recording_mode": mode}
        )

    async def async_connect_websocket(
        self, path: str = DEFAULT_WEBSOCKET_PATH, *, since: int | None = None
    ) -> ClientWebSocketResponse:
        """Open the event socket, optionally resuming after ``since``.

        The token goes in the ``Authorization`` header rather than the ``?token=``
        query parameter Sentinel also accepts: a query string ends up in access
        logs, a header does not.
        """
        url = websocket_url(self.base_url, path, since=since)
        try:
            async with asyncio.timeout(self._request_timeout):
                return await self._session.ws_connect(
                    url,
                    headers=self._auth_headers,
                    heartbeat=WEBSOCKET_HEARTBEAT_SECONDS,
                )
        except TimeoutError as err:
            raise SentinelConnectionError(
                "Timed out opening the Sentinel event socket"
            ) from err
        except ClientError as err:
            status = getattr(err, "status", None)
            if status in (401, 403):
                raise SentinelAuthError("The Sentinel API token was rejected") from err
            raise SentinelConnectionError(
                f"Cannot open the event socket at {url}"
            ) from err


class SentinelEventStream:
    """Keeps the event socket alive for as long as it is running.

    Reconnects with exponential backoff and, crucially, never stops trying: a
    Sentinel restart, a network outage or a server that is not listening yet must
    all end in the entities coming back on their own.
    """

    def __init__(
        self,
        client: SentinelClient,
        *,
        path: str = DEFAULT_WEBSOCKET_PATH,
        on_event: Callable[[SentinelEvent], None],
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        backoff_base: float = RECONNECT_BASE_SECONDS,
        backoff_max: float = RECONNECT_MAX_SECONDS,
    ) -> None:
        self._client = client
        self._path = path
        self._on_event = on_event
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._connected = False
        self._websocket: ClientWebSocketResponse | None = None
        self._last_seq: int | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_seq(self) -> int | None:
        """Highest sequence number seen so far, or ``None`` before the first event."""
        return self._last_seq

    async def async_run(self) -> None:
        """Connect, dispatch frames, reconnect. Returns only when cancelled."""
        attempt = 0
        while True:
            try:
                await self._async_listen_once()
            except asyncio.CancelledError:
                raise
            except SentinelAuthError:
                # The token was revoked or rotated. Retrying fast would only add
                # failed logins to the server's log; the REST refresh will raise
                # the same error and start Home Assistant's reauthentication.
                _LOGGER.debug("Sentinel rejected the token on the event socket")
                attempt = _MAX_BACKOFF_EXPONENT
            except SentinelError as err:
                _LOGGER.debug("Sentinel event socket unavailable: %s", err)
            except Exception:
                _LOGGER.exception("Unexpected error on the Sentinel event socket")
            else:
                # A clean close is a normal restart of the server, so start the
                # backoff from scratch instead of inheriting the previous delay.
                attempt = 0
            finally:
                self._set_connected(False)

            delay = reconnect_delay(
                attempt, base=self._backoff_base, maximum=self._backoff_max
            )
            # Jitter keeps several Home Assistant instances from reconnecting in
            # lockstep after a server restart. Not security-sensitive.
            delay += random.uniform(0, delay * 0.25)
            attempt += 1
            await asyncio.sleep(delay)

    async def async_stop(self) -> None:
        """Close the socket so a cancelled run does not leave it half open."""
        websocket = self._websocket
        self._websocket = None
        if websocket is not None and not websocket.closed:
            await websocket.close()

    async def _async_listen_once(self) -> None:
        websocket = await self._client.async_connect_websocket(
            self._path, since=self._last_seq
        )
        self._websocket = websocket
        ping_task = asyncio.create_task(
            self._async_send_application_pings(websocket),
            name="sentinel-websocket-ping",
        )
        try:
            self._set_connected(True)
            async for message in websocket:
                if message.type is WSMsgType.TEXT:
                    self._handle_text(message.data)
                elif message.type is WSMsgType.ERROR:
                    raise SentinelConnectionError("The Sentinel event socket failed")
        finally:
            ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ping_task
            self._websocket = None
            self._set_connected(False)
            if not websocket.closed:
                await websocket.close()

    @staticmethod
    async def _async_send_application_pings(
        websocket: ClientWebSocketResponse,
    ) -> None:
        """Keep Sentinel's application-level client-idle timer refreshed."""
        while True:
            await asyncio.sleep(WEBSOCKET_APPLICATION_PING_SECONDS)
            await websocket.send_json({"type": "ping"})

    def _handle_text(self, raw: str) -> None:
        try:
            event = SentinelEvent.from_payload(_json_loads(raw))
        except ValueError:
            # A frame this integration cannot read is not a reason to drop the
            # connection; the next one is probably fine.
            _LOGGER.debug("Ignoring an unreadable Sentinel frame")
            return
        if event.seq is not None:
            self._last_seq = event.seq
        self._on_event(event)

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        callback = self._on_connect if connected else self._on_disconnect
        if callback is not None:
            callback()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.async_stop()


def _json_loads(raw: str) -> Any:
    import json

    return json.loads(raw)
