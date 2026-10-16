"""Persistent, proxy-aware protection for public administrator sign-in.

The public hostname is allowed to identify a visitor with ``CF-Connecting-IP``
only when the TCP peer is an explicitly trusted proxy.  Direct LAN sign-in does
not participate in permanent blocking and remains the recovery path.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.config import Settings
from app.database.base import utcnow
from app.database.models import LoginClientState
from app.system import settings_store

CF_CONNECTING_IP_HEADER = "CF-Connecting-IP"
MIN_BLOCK_SECONDS = 60
MAX_BLOCK_SECONDS = 365 * 24 * 60 * 60
MAX_FAILURE_THRESHOLD = 20


@dataclass(frozen=True, slots=True)
class LoginSecurityPolicy:
    enabled: bool
    failure_threshold: int
    block_duration_seconds: int | None


@dataclass(frozen=True, slots=True)
class LoginClient:
    address: str
    public_hostname: str | None
    is_public: bool
    is_verified: bool
    verification_error: str | None = None


def canonical_ip(value: str | None) -> str | None:
    if value is None or len(value) > 64:
        return None
    candidate = value.strip()
    if not candidate or "," in candidate or "%" in candidate:
        return None
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return address.compressed


def _hostname(connection: HTTPConnection) -> str:
    return (connection.url.hostname or "").rstrip(".").lower()


def _trusted_proxy(peer: str | None, settings: Settings) -> bool:
    address_text = canonical_ip(peer)
    if address_text is None:
        return False
    address = ipaddress.ip_address(address_text)
    return any(
        address in ipaddress.ip_network(network, strict=False)
        for network in settings.trusted_proxy_cidrs
    )


def resolve_login_client(connection: HTTPConnection, settings: Settings) -> LoginClient:
    """Resolve the rate-limit/blocking identity for one login request.

    A public hostname with a missing or untrusted proxy is marked unverified so
    an enabled policy can fail closed instead of blocking the tunnel peer or
    accepting a spoofed header.
    """

    peer = canonical_ip(connection.client.host if connection.client else None) or "unknown"
    hostname = _hostname(connection)
    public_hosts = {host.rstrip(".").lower() for host in settings.viewer_trusted_hosts}
    if hostname not in public_hosts:
        return LoginClient(
            address=peer,
            public_hostname=None,
            is_public=False,
            is_verified=False,
        )

    if not _trusted_proxy(peer, settings):
        return LoginClient(
            address=peer,
            public_hostname=hostname,
            is_public=True,
            is_verified=False,
            verification_error="The public login did not arrive through a trusted proxy.",
        )

    visitor = canonical_ip(connection.headers.get(CF_CONNECTING_IP_HEADER))
    if visitor is None:
        return LoginClient(
            address=peer,
            public_hostname=hostname,
            is_public=True,
            is_verified=False,
            verification_error="Cloudflare did not provide a valid visitor address.",
        )

    return LoginClient(
        address=visitor,
        public_hostname=hostname,
        is_public=True,
        is_verified=True,
    )


def proxy_configuration_ready(settings: Settings) -> bool:
    return bool(settings.viewer_trusted_hosts and settings.trusted_proxy_cidrs)


def _normalise_policy(raw: object) -> LoginSecurityPolicy:
    values = raw if isinstance(raw, dict) else {}
    try:
        threshold = int(values.get("failure_threshold", 1))
    except (TypeError, ValueError):
        threshold = 1
    threshold = min(MAX_FAILURE_THRESHOLD, max(1, threshold))

    raw_duration = values.get("block_duration_seconds")
    try:
        duration = None if raw_duration is None else int(raw_duration)
    except (TypeError, ValueError):
        duration = None
    if duration is not None:
        duration = min(MAX_BLOCK_SECONDS, max(MIN_BLOCK_SECONDS, duration))

    return LoginSecurityPolicy(
        enabled=bool(values.get("enabled", False)),
        failure_threshold=threshold,
        block_duration_seconds=duration,
    )


async def get_policy(session: AsyncSession) -> LoginSecurityPolicy:
    raw = await settings_store.get_setting(
        session,
        settings_store.LOGIN_IP_BLOCK,
        settings_store.DEFAULTS[settings_store.LOGIN_IP_BLOCK],
    )
    return _normalise_policy(raw)


async def set_policy(session: AsyncSession, policy: LoginSecurityPolicy) -> None:
    await settings_store.set_setting(
        session,
        settings_store.LOGIN_IP_BLOCK,
        {
            "enabled": policy.enabled,
            "failure_threshold": policy.failure_threshold,
            "block_duration_seconds": policy.block_duration_seconds,
        },
    )


def block_is_active(row: LoginClientState) -> bool:
    if row.blocked_at is None:
        return False
    return row.blocked_until is None or row.blocked_until > utcnow()


async def get_active_block(session: AsyncSession, address: str) -> LoginClientState | None:
    row = await session.get(LoginClientState, address)
    if row is None or row.blocked_at is None:
        return None
    if block_is_active(row):
        return row
    await session.delete(row)
    await session.flush()
    return None


async def record_failure(
    session: AsyncSession,
    *,
    address: str,
    public_hostname: str,
    policy: LoginSecurityPolicy,
) -> LoginClientState:
    now = utcnow()
    row = await session.get(LoginClientState, address)
    if row is None or (row.blocked_at is not None and not block_is_active(row)):
        if row is not None:
            await session.delete(row)
            await session.flush()
        row = LoginClientState(
            ip_address=address,
            public_hostname=public_hostname,
            failed_attempts=0,
            first_failed_at=now,
            last_failed_at=now,
            updated_at=now,
        )
        session.add(row)

    row.public_hostname = public_hostname
    row.failed_attempts += 1
    row.last_failed_at = now
    row.updated_at = now
    if row.failed_attempts >= policy.failure_threshold and row.blocked_at is None:
        row.blocked_at = now
        row.blocked_until = (
            now + timedelta(seconds=policy.block_duration_seconds)
            if policy.block_duration_seconds is not None
            else None
        )
    await session.flush()
    return row


async def clear_pending_failure(session: AsyncSession, address: str) -> None:
    row = await session.get(LoginClientState, address)
    if row is not None and row.blocked_at is None:
        await session.delete(row)
        await session.flush()


async def list_active_blocks(session: AsyncSession) -> list[LoginClientState]:
    result = await session.execute(
        select(LoginClientState)
        .where(LoginClientState.blocked_at.is_not(None))
        .order_by(LoginClientState.blocked_at.desc())
    )
    active: list[LoginClientState] = []
    for row in result.scalars():
        if block_is_active(row):
            active.append(row)
        else:
            await session.delete(row)
    await session.flush()
    return active


async def unblock(session: AsyncSession, address: str) -> bool:
    canonical = canonical_ip(address)
    if canonical is None:
        return False
    row = await session.get(LoginClientState, canonical)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def clear_all_states(session: AsyncSession) -> int:
    count = await session.scalar(select(func.count()).select_from(LoginClientState))
    await session.execute(delete(LoginClientState))
    await session.flush()
    return int(count or 0)
