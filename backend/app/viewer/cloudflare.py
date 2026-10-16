"""Bounded verification of Cloudflare Access application JWTs.

Only deployment configuration chooses the issuer and JWKS URL. A verified
Access assertion is a bearer grant; proxy identity headers provide no proof.
"""

from __future__ import annotations

import asyncio
import json
import time
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from httpx import AsyncClient, HTTPError

from app.config import Settings

_MAX_TOKEN_BYTES = 16_384
_MAX_JWKS_BYTES = 262_144
_MAX_KEYS = 16
_CACHE_SECONDS = 300.0
_REFRESH_COOLDOWN_SECONDS = 30.0
_FETCH_TIMEOUT_SECONDS = 5.0


class CloudflareVerifier:
    """One issuer's public keys, with coalesced fetches and bounded retries.

    Unknown kids can refresh a fresh cache, at most once every 30 seconds.
    During a JWKS outage, existing keys remain valid only until their original
    five-minute expiry. Expired keys are never used as an outage fallback.
    """

    def __init__(self, issuer: str) -> None:
        self._jwks_url = f"{issuer}/cdn-cgi/access/certs"
        self._keys: dict[str, RSAPublicKey] = {}
        self._expires_at = 0.0
        self._retry_after = 0.0
        self._lock = asyncio.Lock()

    async def _fetch_keys(self) -> dict[str, RSAPublicKey]:
        # Disable environment proxies and redirects so a token cannot steer
        # retrieval, and cap the entire operation as well as individual I/O.
        async with asyncio.timeout(_FETCH_TIMEOUT_SECONDS):
            async with AsyncClient(timeout=3.0, follow_redirects=False, trust_env=False) as client:
                async with client.stream("GET", self._jwks_url) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16_384):
                        body.extend(chunk)
                        if len(body) > _MAX_JWKS_BYTES:
                            raise ValueError("Access JWKS response exceeds size limit")
        document = json.loads(body)
        entries = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_KEYS:
            raise ValueError("Invalid Access JWKS")
        keys: dict[str, RSAPublicKey] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Invalid Access JWK")
            if (
                entry.get("kty") != "RSA"
                or entry.get("alg", "RS256") != "RS256"
                or entry.get("use", "sig") != "sig"
            ):
                continue
            key_ops = entry.get("key_ops")
            if key_ops is not None and (not isinstance(key_ops, list) or "verify" not in key_ops):
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or not 1 <= len(kid) <= 128 or kid in keys:
                raise ValueError("Invalid or duplicate Access key identifier")
            key = jwt.PyJWK(entry, algorithm="RS256").key
            if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
                raise ValueError("Access signing key must be RSA with at least 2048 bits")
            keys[kid] = key
        if not keys:
            raise ValueError("No supported Access signing keys")
        return keys

    async def signing_key(self, kid: str) -> RSAPublicKey | None:
        now = time.monotonic()
        if now < self._expires_at and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            now = time.monotonic()
            if now < self._expires_at and kid in self._keys:
                return self._keys[kid]
            if now < self._retry_after:
                return None
            self._retry_after = now + _REFRESH_COOLDOWN_SECONDS
            try:
                keys = await self._fetch_keys()
            except (HTTPError, TimeoutError, ValueError, TypeError, RecursionError, jwt.PyJWTError):
                return None
            self._keys = keys
            self._expires_at = time.monotonic() + _CACHE_SECONDS
            return keys.get(kid)


@lru_cache(maxsize=4)
def _verifier(issuer: str) -> CloudflareVerifier:
    return CloudflareVerifier(issuer)


async def valid_cloudflare_assertion(assertion: str | None, settings: Settings) -> bool:
    issuer = settings.viewer_cloudflare_issuer
    audiences = settings.viewer_cloudflare_audiences
    if not issuer or not audiences or not assertion or len(assertion) > _MAX_TOKEN_BYTES:
        return False
    try:
        header = jwt.get_unverified_header(assertion)
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not 1 <= len(kid) <= 128
            or header.get("crit")
            or "b64" in header
        ):
            return False
        key = await _verifier(issuer).signing_key(kid)
        if key is None:
            return False
        jwt.decode(
            assertion,
            key=key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audiences,
            options={"require": ["exp", "iss", "aud"]},
        )
    except (jwt.PyJWTError, ValueError, TypeError, OverflowError, RecursionError):
        return False
    return True
