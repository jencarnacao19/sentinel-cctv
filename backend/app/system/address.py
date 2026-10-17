"""Telling a person where to open Sentinel.

After ``docker compose up`` the only thing anybody reads is the log, so the
log says which address to open in a browser: the setup wizard until setup is
finished, and the camera wall after that.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import ifaddr

from app.logging_config import get_logger

logger = get_logger("SYSTEM")

#: Interfaces that belong to containers, VPNs and virtual machines on this
#: host rather than to the network the household is on.
_VIRTUAL_PREFIXES = ("docker", "br-", "veth", "virbr", "cni", "flannel", "tailscale", "zt", "wg")

#: Docker's default bridge pool. Inside a bridged container this is the only
#: address there is, and nobody on the LAN can open it.
_DOCKER_BRIDGES = ipaddress.ip_network("172.16.0.0/12")


def _in_container() -> bool:
    return Path("/.dockerenv").exists()


def lan_addresses() -> list[str]:
    """IPv4 addresses another device on the network can reach, best first."""
    try:
        adapters = ifaddr.get_adapters()
    except OSError:
        return []

    found: set[ipaddress.IPv4Address] = set()
    for adapter in adapters:
        name = str(adapter.nice_name or adapter.name).lower()
        if name.startswith(_VIRTUAL_PREFIXES):
            continue
        for ip in adapter.ips:
            if not ip.is_IPv4 or not isinstance(ip.ip, str):
                continue
            try:
                parsed = ipaddress.IPv4Address(ip.ip)
            except ValueError:
                continue
            if parsed.is_private and not (parsed.is_loopback or parsed.is_link_local):
                found.add(parsed)

    if _in_container() and found and all(address in _DOCKER_BRIDGES for address in found):
        # A bridged container: its own address means nothing outside the host.
        return []
    # Home networks are almost always 192.168.x.y, so those lead.
    return [str(address) for address in sorted(found, key=lambda a: (a.packed[0] != 192, a))]


def browser_urls(port: int) -> list[str]:
    return [f"http://{address}:{port}" for address in lan_addresses()]


def announce(port: int, *, setup_completed: bool) -> None:
    """Log where to open Sentinel, in words a person acts on."""
    urls = browser_urls(port)
    where = " or ".join(urls) if urls else f"http://<this server's address>:{port}"
    if setup_completed:
        logger.info("Sentinel is open at %s", where)
    else:
        logger.info("Open %s in a browser to set up Sentinel", where)
