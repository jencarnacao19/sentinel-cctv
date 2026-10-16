"""Transport details that keep the realtime connection healthy."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from custom_components.sentinel import api as api_module
from custom_components.sentinel.api import SentinelEventStream


async def test_application_ping_is_sent_periodically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, str]] = []
    ping_seen = asyncio.Event()

    class FakeWebsocket:
        async def send_json(self, payload: dict[str, str]) -> None:
            sent.append(payload)
            ping_seen.set()

    monkeypatch.setattr(api_module, "WEBSOCKET_APPLICATION_PING_SECONDS", 0.001)
    task = asyncio.create_task(
        SentinelEventStream._async_send_application_pings(FakeWebsocket())  # type: ignore[arg-type]
    )
    try:
        await asyncio.wait_for(ping_seen.wait(), timeout=1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert sent[0] == {"type": "ping"}
