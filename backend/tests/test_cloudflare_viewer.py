"""Origin verification uses real RSA signatures and a mocked JWKS transport."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient, MockTransport, Request, Response
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.config import Settings
from app.viewer import cloudflare
from app.viewer.auth import CF_ACCESS_ASSERTION_HEADER, VIEWER_KEY_HEADER, authorise_viewer

ISSUER = "https://sentinel-test.cloudflareaccess.com"
AUDIENCE = "sentinel-viewer-test-audience"
HOST = "viewer.example.test"


@pytest.fixture(scope="module")
def signing_keys() -> tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]:
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))


def _jwk(key: rsa.RSAPrivateKey, kid: str = "current") -> dict:
    return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": kid}


def _token(key: rsa.RSAPrivateKey, *, kid: str = "current", **claims: object) -> str:
    payload = {"iss": ISSUER, "aud": [AUDIENCE], "exp": int(time.time()) + 300, **claims}
    # Sign raw JSON so malformed claims can reach the decoder even when the
    # high-level JWT encoder refuses to produce them (e.g. a null issuer).
    return jwt.api_jws.encode(
        json.dumps(payload).encode(), key, algorithm="RS256", headers={"kid": kid}
    )


@dataclass
class JwksServer:
    body: object
    status: int = 200
    requests: list[Request] = field(default_factory=list)

    async def handle(self, request: Request) -> Response:
        self.requests.append(request)
        await asyncio.sleep(0)
        if isinstance(self.body, bytes):
            return Response(self.status, content=self.body)
        return Response(self.status, json=self.body)


@pytest.fixture
def jwks(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    signing_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> Iterator[JwksServer]:
    settings.viewer_enabled = True
    settings.viewer_trusted_hosts = [HOST]
    settings.viewer_cloudflare_issuer = ISSUER
    settings.viewer_cloudflare_audiences = [AUDIENCE]
    server = JwksServer({"keys": [_jwk(signing_keys[0])]})
    transport = MockTransport(server.handle)

    def make_client(**kwargs: object) -> AsyncClient:
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return AsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(cloudflare, "AsyncClient", make_client)
    cloudflare._verifier.cache_clear()
    yield server
    cloudflare._verifier.cache_clear()


@pytest.mark.parametrize(
    "issuer",
    [
        "http://sentinel-test.cloudflareaccess.com",
        "https://evil.example",
        "https://sentinel-test.cloudflareaccess.com.evil.example",
        "https://cloudflareaccess.com",
        "https://sentinel-test.cloudflareaccess.com@evil.example",
        "https://sentinel-test.cloudflareaccess.com:8443",
        "https://sentinel-test.cloudflareaccess.com/certs",
        "https://sentinel-test.cloudflareaccess.com?url=evil",
        "https://sentinel-test.cloudflareaccess.com#fragment",
        "https://127.0.0.1",
    ],
)
def test_issuer_is_a_configured_cloudflare_https_origin(issuer: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, viewer_cloudflare_issuer=issuer)


def test_cloudflare_configuration_parses_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_VIEWER_CLOUDFLARE_ISSUER", f"{ISSUER}/")
    monkeypatch.setenv("SENTINEL_VIEWER_CLOUDFLARE_AUDIENCES", "first, second, first")
    settings = Settings(_env_file=None)
    assert settings.viewer_cloudflare_issuer == ISSUER
    assert settings.viewer_cloudflare_audiences == ["first", "second"]


async def test_signed_assertion_is_viewer_only(
    client: AsyncClient, settings: Settings, jwks: JwksServer, signing_keys: tuple
) -> None:
    client.headers.update({"host": HOST, CF_ACCESS_ASSERTION_HEADER: _token(signing_keys[0])})
    assert (await client.get("/api/v1/viewer/cameras")).status_code == 200
    assert (await client.get("/api/v1/cameras")).status_code == 401
    assert len(jwks.requests) == 1
    assert str(jwks.requests[0].url) == f"{ISSUER}/cdn-cgi/access/certs"
    client.headers["host"] = "untrusted.example.test"
    assert (await client.get("/api/v1/viewer/cameras")).status_code == 401
    assert len(jwks.requests) == 1


async def test_websocket_uses_the_same_signature_validation(
    settings: Settings, jwks: JwksServer, signing_keys: tuple
) -> None:
    connection = HTTPConnection(
        {
            "type": "websocket",
            "scheme": "wss",
            "path": "/viewer-ws",
            "query_string": b"",
            "headers": [
                (b"host", HOST.encode()),
                (CF_ACCESS_ASSERTION_HEADER.lower().encode(), _token(signing_keys[0]).encode()),
            ],
        }
    )
    async with AsyncSession() as session:
        grant = await authorise_viewer(connection, session, settings)
    assert grant.kind == "cloudflare_access"
    assert len(jwks.requests) == 1


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://another-team.cloudflareaccess.com"},
        {"aud": ["a-different-application"]},
        {"exp": 1},
        {"exp": None},
        {"iss": None},
        {"aud": None},
        {"exp": float("inf")},
        {"nbf": 99_999_999_999},
        {"iat": 99_999_999_999},
    ],
)
async def test_invalid_signed_claims_fail_closed(
    client: AsyncClient, jwks: JwksServer, signing_keys: tuple, claims: dict
) -> None:
    response = await client.get(
        "/api/v1/viewer/cameras",
        headers={"host": HOST, CF_ACCESS_ASSERTION_HEADER: _token(signing_keys[0], **claims)},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("missing", ["exp", "aud", "iss"])
async def test_required_claims_cannot_be_omitted(
    settings: Settings, jwks: JwksServer, signing_keys: tuple, missing: str
) -> None:
    claims = {"iss": ISSUER, "aud": [AUDIENCE], "exp": int(time.time()) + 60}
    del claims[missing]
    assertion = jwt.encode(claims, signing_keys[0], algorithm="RS256", headers={"kid": "current"})
    assert not await cloudflare.valid_cloudflare_assertion(assertion, settings)


async def test_forged_and_unknown_kid_assertions_fail_closed(
    client: AsyncClient, jwks: JwksServer, signing_keys: tuple
) -> None:
    for assertion in (
        _token(signing_keys[1]),
        _token(signing_keys[1], kid="unknown"),
        "opaque-test-assertion",
        "x" * 16_385,
        jwt.encode({"exp": int(time.time()) + 60}, "x" * 32, algorithm="HS256"),
        jwt.encode({"exp": int(time.time()) + 60}, None, algorithm="none"),
    ):
        response = await client.get(
            "/api/v1/viewer/cameras",
            headers={"host": HOST, CF_ACCESS_ASSERTION_HEADER: assertion},
        )
        assert response.status_code == 401
    assert len(jwks.requests) == 1


async def test_header_key_urls_are_never_followed(
    settings: Settings, jwks: JwksServer, signing_keys: tuple
) -> None:
    assertion = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 60},
        signing_keys[0],
        algorithm="RS256",
        headers={"kid": "current", "jku": "http://127.0.0.1/secret", "x5u": "https://evil.test"},
    )
    assert await cloudflare.valid_cloudflare_assertion(assertion, settings)
    assert [str(request.url) for request in jwks.requests] == [f"{ISSUER}/cdn-cgi/access/certs"]


async def test_deeply_nested_header_is_rejected_without_a_server_error(
    settings: Settings, jwks: JwksServer
) -> None:
    raw = b'{"nested":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}"
    header = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert not await cloudflare.valid_cloudflare_assertion(f"{header}.e30.invalid", settings)
    assert not jwks.requests


@pytest.mark.parametrize("unconfigured", ["issuer", "audiences"])
async def test_unconfigured_cloudflare_does_not_disable_independent_viewer_key(
    client: AsyncClient,
    settings: Settings,
    jwks: JwksServer,
    signing_keys: tuple,
    unconfigured: str,
) -> None:
    if unconfigured == "issuer":
        settings.viewer_cloudflare_issuer = None
    else:
        settings.viewer_cloudflare_audiences = []
    client.headers.update({"host": HOST, CF_ACCESS_ASSERTION_HEADER: _token(signing_keys[0])})
    assert (await client.get("/api/v1/viewer/cameras")).status_code == 401
    settings.viewer_key = SecretStr("viewer-key" * 5)
    client.headers[VIEWER_KEY_HEADER] = "viewer-key" * 5
    assert (await client.get("/api/v1/viewer/cameras")).status_code == 200
    assert not jwks.requests


async def test_key_rotation_refreshes_once_and_unknown_kids_cannot_stampede(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, jwks: JwksServer, signing_keys: tuple
) -> None:
    now = [1000.0]
    monkeypatch.setattr(cloudflare, "time", SimpleNamespace(monotonic=lambda: now[0]))
    initial = _token(signing_keys[0])
    assert all(
        await asyncio.gather(
            *(cloudflare.valid_cloudflare_assertion(initial, settings) for _ in range(20))
        )
    )
    assert len(jwks.requests) == 1
    unknown = _token(signing_keys[1], kid="rotated")
    assert not await cloudflare.valid_cloudflare_assertion(unknown, settings)
    assert len(jwks.requests) == 1
    now[0] += 31
    jwks.body = {"keys": [_jwk(signing_keys[1], "rotated")]}
    assert all(
        await asyncio.gather(
            *(cloudflare.valid_cloudflare_assertion(unknown, settings) for _ in range(20))
        )
    )
    assert len(jwks.requests) == 2
    assert not await cloudflare.valid_cloudflare_assertion(initial, settings)
    assert len(jwks.requests) == 2


async def test_jwks_outage_never_extends_expired_cache(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, jwks: JwksServer, signing_keys: tuple
) -> None:
    now = [1000.0]
    monkeypatch.setattr(cloudflare, "time", SimpleNamespace(monotonic=lambda: now[0]))
    assertion = _token(signing_keys[0])
    assert await cloudflare.valid_cloudflare_assertion(assertion, settings)
    jwks.status = 503
    now[0] += 100
    assert await cloudflare.valid_cloudflare_assertion(assertion, settings)
    now[0] += 201
    assert not any(
        await asyncio.gather(
            *(cloudflare.valid_cloudflare_assertion(assertion, settings) for _ in range(20))
        )
    )
    assert len(jwks.requests) == 2


@pytest.mark.parametrize(
    "body,status",
    [(b"not json", 200), ({"keys": []}, 200), (b"x" * 262_145, 200), ({}, 302), ({}, 503)],
    ids=["invalid-json", "empty-keys", "oversized", "redirect", "unavailable"],
)
async def test_jwks_failures_deny_without_repeated_fetches(
    settings: Settings, jwks: JwksServer, signing_keys: tuple, body: object, status: int
) -> None:
    jwks.body, jwks.status = body, status
    assertion = _token(signing_keys[0])
    assert not any(
        await asyncio.gather(
            *(cloudflare.valid_cloudflare_assertion(assertion, settings) for _ in range(10))
        )
    )
    assert len(jwks.requests) == 1


async def test_network_timeout_is_bounded_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, jwks: JwksServer, signing_keys: tuple
) -> None:
    async def stalled(request: Request) -> Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(cloudflare, "_FETCH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        cloudflare, "AsyncClient", lambda **kwargs: AsyncClient(transport=MockTransport(stalled))
    )
    async with asyncio.timeout(1):
        assert not await cloudflare.valid_cloudflare_assertion(_token(signing_keys[0]), settings)


async def test_machine_token_cannot_bypass_scopes_through_viewer_ptz_or_export(
    admin_client: AsyncClient, settings: Settings
) -> None:
    created = await admin_client.post(
        "/api/v1/auth/tokens", json={"name": "read-only integration", "scope": "read_only"}
    )
    assert created.status_code == 201
    admin_client.headers["Authorization"] = f"Bearer {created.json()['token']}"
    admin_client.cookies.clear()
    settings.viewer_enabled = True
    settings.viewer_trusted_hosts = [HOST]
    admin_client.headers.update({"host": HOST, CF_ACCESS_ASSERTION_HEADER: "opaque-forged-token"})
    assert (await admin_client.get("/api/v1/viewer/cameras")).status_code == 401
    assert (await admin_client.post("/api/v1/viewer/cameras/example/ptz/stop")).status_code == 401
    start = datetime.now(UTC) - timedelta(minutes=1)
    assert (
        await admin_client.post(
            "/api/v1/viewer/exports",
            json={
                "camera_id": "example",
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(seconds=30)).isoformat(),
            },
        )
    ).status_code == 401
    # An independent, explicitly configured viewer credential grants its own
    # operator permissions; the API token itself still grants none of them.
    settings.viewer_key = SecretStr("explicit-viewer-key" * 3)
    admin_client.headers[VIEWER_KEY_HEADER] = "explicit-viewer-key" * 3
    assert (await admin_client.get("/api/v1/viewer/cameras")).status_code == 200
