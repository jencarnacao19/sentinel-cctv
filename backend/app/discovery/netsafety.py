"""Keeping "scan my network" from turning into "fetch this URL for me".

Cameras are added today by pasting an RTSP URL, and ``app.cameras.rtsp``
deliberately does *not* restrict which host that URL points at -- Sentinel is
a LAN application and has to be able to reach whatever address the operator
typed. Discovery is a different shape of feature: it takes a bare host (or an
address volunteered by an untrusted UDP multicast reply) and *automatically*
issues a sequence of requests against it -- ONVIF calls, vendor URL guesses,
NVR channel sweeps -- with no human reviewing each one first. That automation
is exactly what makes it an SSRF vector if left unrestricted: a "channel scan"
that will happily walk sixty-four paths against an address the caller chose
is a generic HTTP/RTSP request tool with a discovery feature's UI on it.
So everything in this package that connects to a *caller-supplied* host goes
through here first. WS-Discovery itself never does: it only ever sends to the
fixed multicast group, never to an address a caller or a reply chose.
"""

from __future__ import annotations

import asyncio
import ipaddress

#: How long DNS resolution gets before it counts as "unreachable" rather than
#: waiting on the caller's whole request budget.
RESOLVE_TIMEOUT_SECONDS = 5.0


class UnscannableHostError(ValueError):
    """The target does not look like it lives on a private network."""


#: RFC 1918 -- the only IPv4 space this function treats as "your own LAN".
_RFC1918_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)

#: RFC 4193 unique-local space -- IPv6's equivalent of RFC 1918.
_IPV6_UNIQUE_LOCAL = ipaddress.IPv6Network("fc00::/7")


def _is_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether ``address`` is the kind of address discovery may touch.

    Deliberately narrower than :attr:`ipaddress.IPv4Address.is_private`. That
    stdlib flag follows the IANA special-purpose registry, which also marks
    the TEST-NET documentation ranges (``192.0.2.0/24``, ``198.51.100.0/24``,
    ``203.0.113.0/24``) and several other non-forwardable-but-not-LAN blocks
    as "private" -- true in the sense that they are reserved and will never
    appear on the public internet, but a different property from "lives on
    the caller's own network". A guard whose whole job is confining scans to
    the caller's LAN cannot trust that flag: a hostname engineered (or simply
    misconfigured) to resolve into a documentation range would otherwise sail
    straight through it.
    """
    if address.is_loopback or address.is_link_local:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _RFC1918_NETWORKS)
    return address in _IPV6_UNIQUE_LOCAL


async def ensure_scannable_host(host: str) -> None:
    """Resolve ``host`` and refuse it unless every address it resolves to is
    private, link-local or loopback.

    Resolving before checking -- rather than pattern-matching the hostname
    string -- is what stops a DNS name that resolves to a public address from
    being used to make Sentinel probe the wider internet. This does not close
    every DNS-rebinding window (a name could resolve differently a few
    milliseconds later, when the actual FFprobe or ONVIF connection is made);
    closing that fully would mean pinning every subsequent socket to the
    address resolved here, which none of FFmpeg, FFprobe or httpx's default
    transport lets a caller do without reimplementing connection handling.
    For a tool whose whole job is *inspecting your own LAN*, resolve-then-
    check is a proportionate defence against the common case -- a typo'd or
    malicious public hostname -- without pretending to be airtight.
    """
    for address in await _resolve(host):
        if not _is_private(address):
            raise UnscannableHostError(
                f"'{host}' resolves to {address}, which is not a private address. "
                "Discovery only scans devices on your own network."
            )


async def ensure_onvif_host(host: str) -> None:
    """:func:`ensure_scannable_host`, and never this machine itself.

    ONVIF calls are HTTP POSTs to an address that a device on the network
    chose, starting with the one in an unauthenticated WS-Discovery reply.
    Loopback is where go2rtc's API and Sentinel itself listen, so a reply
    pointing there must never be followed. A camera served from this same
    machine can still be added by its RTSP URL; it just cannot be discovered.
    """
    await ensure_scannable_host(host)
    for address in await _resolve(host):
        if address.is_loopback:
            raise UnscannableHostError(
                f"'{host}' resolves to {address}, which is this machine. "
                "ONVIF discovery only contacts other devices on your network."
            )


async def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        parsed = None

    if parsed is not None:
        addresses = [parsed]
    else:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, None), timeout=RESOLVE_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError) as exc:
            raise UnscannableHostError(
                f"Could not resolve '{host}'. Check the address and try again."
            ) from exc
        if not infos:
            raise UnscannableHostError(f"Could not resolve '{host}'.")
        addresses = []
        for info in infos:
            # The sockaddr tuple's first element is always the address as a
            # string; typeshed's getaddrinfo overloads just aren't precise
            # enough to narrow that from the general sockaddr type.
            raw = str(info[4][0])
            # An IPv6 scope id ("fe80::1%eth0") is not part of the address
            # itself and ipaddress rejects it outright.
            addresses.append(ipaddress.ip_address(raw.split("%", 1)[0]))
    return addresses
