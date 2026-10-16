"""HTTP client for the local go2rtc API.

go2rtc listens on loopback only, so this client never leaves the machine. It is
used for three things: checking that go2rtc is alive, adding and removing
streams without a restart, and pulling a JPEG frame.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from app.config import Settings
from app.logging_config import get_logger

logger = get_logger("GO2RTC")

DEFAULT_TIMEOUT = httpx.Timeout(connect=2.0, read=10.0, write=5.0, pool=2.0)
HLS_TIMEOUT = httpx.Timeout(connect=2.0, read=15.0, write=5.0, pool=2.0)
MAX_HLS_RESPONSE_BYTES = 16 * 1024 * 1024


class Go2RtcUnavailableError(RuntimeError):
    """go2rtc is not answering. The caller decides whether that is fatal."""


class Go2RtcClient:
    """Thin wrapper over go2rtc's REST API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = settings.go2rtc_api_url
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> Go2RtcClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=DEFAULT_TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # --- Queries ---------------------------------------------------------

    async def is_alive(self) -> bool:
        try:
            response = await self._get_client().get("/api", timeout=2.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def info(self) -> dict[str, Any]:
        try:
            response = await self._get_client().get("/api")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise Go2RtcUnavailableError(str(exc)) from exc
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def list_streams(self) -> dict[str, Any]:
        """Return the streams go2rtc currently knows about, keyed by name."""
        try:
            response = await self._get_client().get("/api/streams")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise Go2RtcUnavailableError(str(exc)) from exc
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    # --- Mutations -------------------------------------------------------

    async def add_stream(self, name: str, sources: str | Sequence[str]) -> None:
        """Add or replace a stream without restarting go2rtc.

        This is why adding a camera does not interrupt the live view or the
        recording of every other camera.
        """
        source_list = [sources] if isinstance(sources, str) else list(sources)
        params = httpx.QueryParams([("name", name), *(("src", source) for source in source_list)])
        try:
            response = await self._get_client().put("/api/streams", params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise Go2RtcUnavailableError(str(exc)) from exc

    async def delete_stream(self, name: str) -> None:
        try:
            response = await self._get_client().delete("/api/streams", params={"src": name})
            # A stream that is already gone is not an error worth propagating.
            if response.status_code not in (200, 404):
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise Go2RtcUnavailableError(str(exc)) from exc

    # --- Media -----------------------------------------------------------

    async def webrtc_offer(
        self, name: str, offer: bytes, *, content_type: str = "application/sdp"
    ) -> tuple[bytes, str]:
        """Forward an SDP offer and return ``(answer, content_type)``.

        The stream name comes from the camera record, never from the caller, so
        a client cannot use this to make go2rtc open an arbitrary source.
        """
        try:
            response = await self._get_client().post(
                "/api/webrtc",
                params={"src": name},
                content=offer,
                headers={"Content-Type": content_type},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise Go2RtcUnavailableError(str(exc)) from exc
        return response.content, response.headers.get("content-type", "application/sdp")

    async def snapshot(self, name: str, *, timeout_seconds: float = 10.0) -> bytes:
        """Pull a single JPEG frame from an existing stream.

        Cheaper than opening the camera directly: go2rtc already holds the
        connection, so this costs the camera nothing.
        """
        try:
            response = await self._get_client().get(
                "/api/frame.jpeg", params={"src": name}, timeout=timeout_seconds
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise Go2RtcUnavailableError(str(exc)) from exc
        return response.content

    async def hls_master(self, name: str) -> bytes:
        """Open one fMP4 HLS session for an already-known stream."""
        body, _ = await self._hls_get(
            "/api/stream.m3u8",
            params=[("src", name), ("mp4", "")],
            max_bytes=256 * 1024,
        )
        return body

    async def hls_resource(
        self,
        resource: str,
        session_id: str,
        *,
        sequence: int | None = None,
    ) -> tuple[bytes, str]:
        """Read one playlist/init/segment from a loopback-only HLS session."""
        paths = {
            "playlist": "/api/hls/playlist.m3u8",
            "init": "/api/hls/init.mp4",
            "segment": "/api/hls/segment.m4s",
        }
        try:
            path = paths[resource]
        except KeyError as exc:  # pragma: no cover - internal programming error
            raise ValueError(f"Unsupported HLS resource: {resource}") from exc
        params: list[tuple[str, str | int | float | bool | None]] = [("id", session_id)]
        if sequence is not None:
            params.append(("n", sequence))
        max_bytes = 512 * 1024 if resource == "playlist" else MAX_HLS_RESPONSE_BYTES
        return await self._hls_get(path, params=params, max_bytes=max_bytes)

    async def _hls_get(
        self,
        path: str,
        *,
        params: list[tuple[str, str | int | float | bool | None]],
        max_bytes: int,
    ) -> tuple[bytes, str]:
        try:
            async with self._get_client().stream(
                "GET", path, params=params, timeout=HLS_TIMEOUT
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise Go2RtcUnavailableError("HLS response exceeded the safety limit")
                content_type = response.headers.get("content-type", "application/octet-stream")
                return bytes(body), content_type
        except httpx.HTTPError as exc:
            raise Go2RtcUnavailableError(str(exc)) from exc
