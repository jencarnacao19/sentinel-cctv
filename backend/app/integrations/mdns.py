"""mDNS announcement of this Sentinel instance as ``_sentinel._tcp.local.``.

Home Assistant's config flow discovers Sentinel through this rather than
requiring a human to type an IP address (`app.integrations.CONTRACT`,
section 2). Multicast is exactly the kind of network feature that
misbehaves on an odd VLAN, a Docker network mode, or a Wi-Fi access point
with client isolation -- so every failure in this module is caught and
logged as a warning. mDNS never gets a vote on whether Sentinel starts.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Final

import ifaddr
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from app import __version__
from app.config import Settings, get_settings
from app.integrations.identity import install_id
from app.logging_config import get_logger

logger = get_logger("HOME_ASSISTANT")

SERVICE_TYPE: Final = "_sentinel._tcp.local."


def _local_ipv4_addresses() -> list[str]:
    """Every non-loopback, non-link-local IPv4 address this host has.

    ``zeroconf.get_all_addresses()`` does the same thing but is deprecated in
    favour of calling ``ifaddr`` directly (a zeroconf dependency already), so
    that is what this does instead of carrying a deprecation warning into
    every start-up log.
    """
    found: set[str] = set()
    try:
        adapters = ifaddr.get_adapters()
    except OSError:
        return []

    for adapter in adapters:
        for ip in adapter.ips:
            if not ip.is_IPv4 or not isinstance(ip.ip, str):
                continue
            try:
                parsed = ipaddress.ip_address(ip.ip)
            except ValueError:
                continue
            if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified:
                continue
            found.add(ip.ip)
    return sorted(found)


def _sanitise_label(value: str) -> str:
    """A DNS-safe label: letters, digits and hyphens only, no leading/trailing hyphen."""
    cleaned = "".join(char if char.isalnum() or char == "-" else "-" for char in value)
    cleaned = cleaned.strip("-")
    return cleaned[:63] or "sentinel"


class MdnsAnnouncer:
    """Owns the announcement's lifecycle. One instance per process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._aiozc: AsyncZeroconf | None = None
        self._info: AsyncServiceInfo | None = None

    async def start(self) -> None:
        """Announce, or log why not. Never raises."""
        if not self._settings.enable_mdns:
            logger.info("mDNS announcement disabled by configuration")
            return
        if self._settings.testing:
            logger.debug("Testing mode: mDNS announcement not started")
            return
        try:
            await self._start()
        except Exception:
            logger.warning(
                "Could not start mDNS announcement; Sentinel is still reachable by "
                "address, only automatic discovery is unavailable",
                exc_info=True,
            )

    async def _start(self) -> None:
        addresses = _local_ipv4_addresses()
        if not addresses:
            logger.warning(
                "No usable network address found; mDNS announcement skipped "
                "(this host may only have link-local addresses)"
            )
            return

        label = _sanitise_label(socket.gethostname().split(".")[0] or "sentinel")
        service_name = f"{label}.{SERVICE_TYPE}"

        info = AsyncServiceInfo(
            SERVICE_TYPE,
            service_name,
            port=self._settings.port,
            properties={"version": __version__, "id": install_id()},
            parsed_addresses=addresses,
            server=f"{label}.local.",
        )

        aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        try:
            await aiozc.async_register_service(info, allow_name_change=True)
        except Exception:
            await aiozc.async_close()
            raise

        self._aiozc = aiozc
        self._info = info
        logger.info(
            "mDNS announcement started",
            extra={"service": service_name, "port": self._settings.port, "addresses": addresses},
        )

    async def stop(self) -> None:
        """Withdraw the announcement. Safe to call even if start() never
        registered anything, and never raises."""
        aiozc, info = self._aiozc, self._info
        self._aiozc = None
        self._info = None
        if aiozc is None:
            return
        try:
            if info is not None:
                await aiozc.async_unregister_service(info)
            await aiozc.async_close()
        except Exception:
            logger.warning("Error while stopping the mDNS announcement", exc_info=True)

    @property
    def is_announced(self) -> bool:
        return self._aiozc is not None

    @property
    def service_name(self) -> str | None:
        """The fully qualified instance name actually registered, once
        ``start()`` has succeeded -- ``None`` before that or if it never did.
        Useful for diagnostics and for a test looking itself up."""
        return self._info.name if self._info is not None else None


_announcer: MdnsAnnouncer | None = None


def get_announcer() -> MdnsAnnouncer:
    global _announcer
    if _announcer is None:
        _announcer = MdnsAnnouncer()
    return _announcer


def set_announcer(announcer: MdnsAnnouncer | None) -> None:
    """Test helper."""
    global _announcer
    _announcer = announcer


async def start() -> None:
    """Lifespan hook: ``await app.integrations.mdns.start()`` on start-up."""
    await get_announcer().start()


async def stop() -> None:
    """Lifespan hook: ``await app.integrations.mdns.stop()`` on shutdown."""
    await get_announcer().stop()
