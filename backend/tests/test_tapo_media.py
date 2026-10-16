"""Exercise the actual Digest bytes against a local fake camera."""

from __future__ import annotations

import asyncio
import hashlib
from urllib.request import parse_http_list, parse_keqv_list

import pytest

from app.cameras.tapo_media import StandaloneMediaSession


@pytest.mark.parametrize("advertised,expected", [("3", "sha256"), (None, "md5")])
async def test_media_negotiates_hash_from_challenge_without_control_port(advertised, expected):
    result = asyncio.get_running_loop().create_future()

    async def camera(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            extension = f',encrypt_type="{advertised}"' if advertised else ""
            writer.write(
                (
                    "HTTP/1.0 401 Unauthorized\r\nContent-Length: 0\r\n"
                    'WWW-Authenticate: Digest realm="test",algorithm="MD5",'
                    f'qop="auth",nonce="fixed-test-nonce",opaque="test"{extension}\r\n\r\n'
                ).encode()
            )
            await writer.drain()
            request = await reader.readuntil(b"\r\n\r\n")
            headers = dict(
                line.split(":", 1) for line in request.decode().split("\r\n") if ":" in line
            )
            auth = parse_keqv_list(parse_http_list(headers["Authorization"].strip()[7:]))
            password_hash = hashlib.new(expected, b"test-password").hexdigest().upper()

            def md(value):
                return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()

            correct = md(
                ":".join(
                    [
                        md(f"admin:test:{password_hash}"),
                        "fixed-test-nonce",
                        auth["nc"],
                        auth["cnonce"],
                        "auth",
                        md("POST:/stream"),
                    ]
                )
            )
            result.set_result(auth["response"] == correct)
            writer.write(
                b"HTTP/1.0 200 OK\r\nContent-Type: multipart/mixed;boundary=--test\r\n"
                b'Key-Exchange: username="admin" nonce="test-key-nonce"\r\n\r\n'
            )
            await writer.drain()
            await reader.read()
        except Exception as exc:  # noqa: BLE001 - deliver server failures to the test
            if not result.done():
                result.set_exception(exc)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(camera, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    media = StandaloneMediaSession("127.0.0.1", "test-password", "", "sha256", port=port)
    try:
        await asyncio.wait_for(media.start(), 3)
        assert await result
        assert media.encryptionMethod == expected
        assert media.started
    finally:
        await media.close()
        server.close()
        await server.wait_closed()


async def test_cancelled_handshake_closes_its_socket():
    connected = asyncio.Event()
    closed = asyncio.Event()

    async def camera(reader, writer):
        connected.set()
        await reader.read()
        closed.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(camera, "127.0.0.1", 0)
    media = StandaloneMediaSession(
        "127.0.0.1", "test-password", "", "sha256", port=server.sockets[0].getsockname()[1]
    )
    task = asyncio.create_task(media.start())
    try:
        await asyncio.wait_for(connected.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await media.close()
        await asyncio.wait_for(closed.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()
