"""Authentication: first-run setup, sign-in, sessions, API tokens, rate limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.auth.ratelimit import login_limiter, reset_all_limiters
from app.auth.tokens import (
    TokenError,
    create_access_token,
    decode_access_token,
    generate_api_token,
    hash_api_token,
)
from app.config import Settings
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME


class TestAccessTokens:
    def test_round_trip(self, settings: Settings) -> None:
        del settings
        token, expires_at = create_access_token("user-123")
        payload = decode_access_token(token)
        assert payload.subject == "user-123"
        assert payload.expires_at == expires_at.replace(microsecond=0)

    def test_expired_token_is_rejected(self, settings: Settings) -> None:
        del settings
        token, _ = create_access_token("user-123", ttl_minutes=1)
        future = datetime.now(UTC) + timedelta(minutes=5)
        with pytest.raises(TokenError, match="expired"):
            decode_access_token(token, now=future)

    def test_tampered_payload_is_rejected(self, settings: Settings) -> None:
        del settings
        token, _ = create_access_token("user-123")
        body, signature = token.split(".")
        forged, _ = create_access_token("attacker")
        with pytest.raises(TokenError, match="signature"):
            decode_access_token(f"{forged.split('.')[0]}.{signature}")
        with pytest.raises(TokenError):
            decode_access_token(f"{body}.{signature[:-2]}xy")

    @pytest.mark.parametrize("value", ["", "junk", "a.b.c", "onlyonepart"])
    def test_malformed_tokens_are_rejected(self, settings: Settings, value: str) -> None:
        del settings
        with pytest.raises(TokenError):
            decode_access_token(value)

    def test_api_token_shape(self) -> None:
        plaintext, prefix, token_hash = generate_api_token()
        assert plaintext.startswith("sctv_")
        assert plaintext.startswith(prefix)
        assert len(prefix) == len("sctv_") + 6
        assert token_hash == hash_api_token(plaintext)
        assert token_hash != plaintext


class TestFirstRunSetup:
    async def test_status_reports_uninitialised(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/auth/status")
        assert response.status_code == 200
        assert response.json() == {"initialised": False, "setup_completed": False}

    async def test_create_admin_then_status_flips(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/setup",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["username"] == ADMIN_USERNAME
        assert body["is_admin"] is True
        assert "password" not in body
        assert "password_hash" not in body

        status_response = await client.get("/api/v1/auth/status")
        assert status_response.json() == {"initialised": True, "setup_completed": False}

    async def test_second_admin_is_refused(self, client: AsyncClient) -> None:
        first = await client.post(
            "/api/v1/auth/setup",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert first.status_code == 201

        second = await client.post(
            "/api/v1/auth/setup",
            json={"username": "intruder", "password": "another-long-password"},
        )
        assert second.status_code == 409

    async def test_weak_password_is_refused(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/setup", json={"username": ADMIN_USERNAME, "password": "short"}
        )
        assert response.status_code == 422


class TestLogin:
    async def test_login_returns_token_and_cookie(self, client: AsyncClient) -> None:
        await client.post(
            "/api/v1/auth/setup",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"]

        cookie = response.cookies.get("sentinel_session")
        assert cookie == body["access_token"]
        set_cookie = response.headers["set-cookie"].lower()
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        assert "path=/" in set_cookie
        assert response.headers["cache-control"] == "no-store"

    async def test_cross_site_cookie_mutation_is_blocked(self, client: AsyncClient) -> None:
        await client.post(
            "/api/v1/auth/setup",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        await client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )

        response = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": ADMIN_PASSWORD,
                "new_password": "a-brand-new-password",
            },
            headers={"Origin": "https://hostile.example", "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Cross-site request blocked."}

    async def test_same_origin_cookie_mutation_is_allowed(self, client: AsyncClient) -> None:
        await client.post(
            "/api/v1/auth/setup",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        await client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )

        response = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": ADMIN_PASSWORD,
                "new_password": "a-brand-new-password",
            },
            headers={"Origin": "http://sentinel.test", "Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 204

    async def test_wrong_password_is_rejected(self, client: AsyncClient) -> None:
        await client.post(
            "/api/v1/auth/setup",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": "wrong-password"},
        )
        assert response.status_code == 401
        assert "password" not in response.json()["detail"].lower().replace(
            "incorrect username or password.", ""
        )

    async def test_unknown_user_is_rejected(self, client: AsyncClient) -> None:
        await client.post(
            "/api/v1/auth/setup",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "ghost", "password": ADMIN_PASSWORD},
        )
        assert response.status_code == 401

    async def test_me_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/auth/me")).status_code == 401

    async def test_me_returns_profile(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/v1/auth/me")
        assert response.status_code == 200
        assert response.json()["username"] == ADMIN_USERNAME

    async def test_invalid_bearer_token_is_rejected(self, client: AsyncClient) -> None:
        client.headers["Authorization"] = "Bearer not.a.token"
        assert (await client.get("/api/v1/auth/me")).status_code == 401

    async def test_logout_clears_cookie(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post("/api/v1/auth/logout")
        assert response.status_code == 204
        assert (
            'sentinel_session=""' in response.headers["set-cookie"]
            or "sentinel_session=;" in response.headers["set-cookie"]
        )


class TestChangePassword:
    async def test_password_can_be_changed(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": ADMIN_PASSWORD,
                "new_password": "a-brand-new-password",
            },
        )
        assert response.status_code == 204

        del admin_client.headers["Authorization"]
        login = await admin_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": "a-brand-new-password"},
        )
        assert login.status_code == 200

    async def test_wrong_current_password_is_rejected(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "nope", "new_password": "a-brand-new-password"},
        )
        assert response.status_code == 401


class TestApiTokens:
    async def test_token_is_returned_once_and_then_usable(self, admin_client: AsyncClient) -> None:
        created = await admin_client.post("/api/v1/auth/tokens", json={"name": "Home Assistant"})
        assert created.status_code == 201
        body = created.json()
        plaintext = body["token"]
        assert plaintext.startswith("sctv_")
        assert body["prefix"] == plaintext[: len("sctv_") + 6]

        listing = await admin_client.get("/api/v1/auth/tokens")
        assert listing.status_code == 200
        rows = listing.json()
        assert len(rows) == 1
        assert "token" not in rows[0], "plaintext must never be listed again"

        del admin_client.headers["Authorization"]
        admin_client.cookies.clear()
        admin_client.headers["Authorization"] = f"Bearer {plaintext}"
        whoami = await admin_client.get("/api/v1/auth/whoami")
        assert whoami.status_code == 200
        assert whoami.json()["kind"] == "api_token"

    async def test_revoked_token_stops_working(self, admin_client: AsyncClient) -> None:
        created = await admin_client.post("/api/v1/auth/tokens", json={"name": "temp"})
        token_id = created.json()["id"]
        plaintext = created.json()["token"]

        revoked = await admin_client.delete(f"/api/v1/auth/tokens/{token_id}")
        assert revoked.status_code == 204

        del admin_client.headers["Authorization"]
        admin_client.cookies.clear()
        admin_client.headers["Authorization"] = f"Bearer {plaintext}"
        assert (await admin_client.get("/api/v1/auth/whoami")).status_code == 401

    async def test_expired_token_is_rejected(self, admin_client: AsyncClient) -> None:
        from sqlalchemy import select

        from app.database.models import ApiToken
        from app.database.session import session_scope

        created = await admin_client.post("/api/v1/auth/tokens", json={"name": "short-lived"})
        plaintext = created.json()["token"]

        async with session_scope() as session:
            token = (await session.execute(select(ApiToken))).scalar_one()
            token.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        del admin_client.headers["Authorization"]
        admin_client.cookies.clear()
        admin_client.headers["Authorization"] = f"Bearer {plaintext}"
        assert (await admin_client.get("/api/v1/auth/whoami")).status_code == 401

    async def test_api_token_cannot_change_a_password(self, admin_client: AsyncClient) -> None:
        created = await admin_client.post("/api/v1/auth/tokens", json={"name": "machine"})
        plaintext = created.json()["token"]

        del admin_client.headers["Authorization"]
        admin_client.cookies.clear()
        admin_client.headers["Authorization"] = f"Bearer {plaintext}"
        response = await admin_client.post(
            "/api/v1/auth/change-password",
            json={"current_password": ADMIN_PASSWORD, "new_password": "another-long-one"},
        )
        assert response.status_code == 403

    async def test_tokens_require_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/auth/tokens")).status_code == 401


class TestRateLimiting:
    async def test_repeated_failures_are_throttled(self, client: AsyncClient) -> None:
        await reset_all_limiters()
        await client.post(
            "/api/v1/auth/setup",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )

        statuses = []
        for _ in range(login_limiter.limit + 2):
            response = await client.post(
                "/api/v1/auth/login",
                json={"username": ADMIN_USERNAME, "password": "wrong"},
            )
            statuses.append(response.status_code)

        assert 429 in statuses, statuses
        assert statuses.index(429) >= login_limiter.limit
        await reset_all_limiters()


class TestPublicLoginBlocking:
    @staticmethod
    def _configure_proxy(settings: Settings) -> None:
        settings.viewer_trusted_hosts = ["cctv.example.com"]
        settings.trusted_proxy_cidrs = ["127.0.0.1/32"]

    @staticmethod
    def _public_headers(address: str) -> dict[str, str]:
        return {
            "Host": "cctv.example.com",
            "CF-Connecting-IP": address,
        }

    async def test_settings_require_an_administrator(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/auth/login-security")).status_code == 401
        assert (await client.get("/api/v1/auth/login-blocks")).status_code == 401

    async def test_protection_cannot_enable_without_a_trusted_proxy(
        self, admin_client: AsyncClient
    ) -> None:
        response = await admin_client.patch(
            "/api/v1/auth/login-security",
            json={
                "enabled": True,
                "failure_threshold": 1,
                "block_duration_seconds": None,
            },
        )
        assert response.status_code == 409

    async def test_one_public_failure_permanently_blocks_only_that_ip(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        self._configure_proxy(settings)
        configured = await admin_client.patch(
            "/api/v1/auth/login-security",
            json={
                "enabled": True,
                "failure_threshold": 1,
                "block_duration_seconds": None,
            },
        )
        assert configured.status_code == 200
        assert configured.json()["enabled"] is True
        assert configured.json()["proxy_ready"] is True

        admin_authorization = admin_client.headers["Authorization"]
        admin_client.headers.pop("Authorization")
        admin_client.cookies.clear()

        failed = await admin_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": "wrong"},
            headers=self._public_headers("203.0.113.44"),
        )
        assert failed.status_code == 401

        refused = await admin_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            headers=self._public_headers("203.0.113.44"),
        )
        assert refused.status_code == 403
        assert "blocked" in refused.json()["detail"].lower()
        assert "retry-after" not in refused.headers

        other_address = await admin_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            headers=self._public_headers("203.0.113.45"),
        )
        assert other_address.status_code == 200

        admin_client.headers["Authorization"] = admin_authorization
        listing = await admin_client.get("/api/v1/auth/login-blocks")
        assert listing.status_code == 200
        assert listing.json() == [
            {
                "ip_address": "203.0.113.44",
                "public_hostname": "cctv.example.com",
                "failed_attempts": 1,
                "first_failed_at": listing.json()[0]["first_failed_at"],
                "last_failed_at": listing.json()[0]["last_failed_at"],
                "blocked_at": listing.json()[0]["blocked_at"],
                "blocked_until": None,
            }
        ]

        removed = await admin_client.delete("/api/v1/auth/login-blocks/203.0.113.44")
        assert removed.status_code == 204

        admin_client.headers.pop("Authorization")
        admin_client.cookies.clear()
        restored = await admin_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            headers=self._public_headers("203.0.113.44"),
        )
        assert restored.status_code == 200

    async def test_direct_lan_failure_never_creates_a_permanent_block(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        self._configure_proxy(settings)
        await admin_client.patch(
            "/api/v1/auth/login-security",
            json={
                "enabled": True,
                "failure_threshold": 1,
                "block_duration_seconds": None,
            },
        )
        admin_authorization = admin_client.headers["Authorization"]
        admin_client.headers.pop("Authorization")
        admin_client.cookies.clear()

        local_failure = await admin_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": "wrong"},
        )
        assert local_failure.status_code == 401

        public_login = await admin_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            headers=self._public_headers("203.0.113.90"),
        )
        assert public_login.status_code == 200

        admin_client.headers["Authorization"] = admin_authorization
        assert (await admin_client.get("/api/v1/auth/login-blocks")).json() == []

    async def test_public_header_from_an_untrusted_peer_fails_closed(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        self._configure_proxy(settings)
        await admin_client.patch(
            "/api/v1/auth/login-security",
            json={
                "enabled": True,
                "failure_threshold": 1,
                "block_duration_seconds": None,
            },
        )
        settings.trusted_proxy_cidrs = ["192.168.50.205/32"]
        admin_client.headers.pop("Authorization")
        admin_client.cookies.clear()

        response = await admin_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            headers=self._public_headers("203.0.113.1"),
        )
        assert response.status_code == 503
        assert "trusted proxy" in response.json()["detail"].lower()

    async def test_success_clears_failures_below_the_threshold(
        self, admin_client: AsyncClient, settings: Settings
    ) -> None:
        self._configure_proxy(settings)
        await admin_client.patch(
            "/api/v1/auth/login-security",
            json={
                "enabled": True,
                "failure_threshold": 2,
                "block_duration_seconds": 900,
            },
        )
        admin_client.headers.pop("Authorization")
        admin_client.cookies.clear()
        headers = self._public_headers("2001:db8::7")

        assert (
            await admin_client.post(
                "/api/v1/auth/login",
                json={"username": ADMIN_USERNAME, "password": "wrong"},
                headers=headers,
            )
        ).status_code == 401
        assert (
            await admin_client.post(
                "/api/v1/auth/login",
                json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
                headers=headers,
            )
        ).status_code == 200
