"""ONVIF WS-Discovery: find cameras without asking anyone to type an address.

A WS-Discovery Probe is sent as a UDP multicast to 239.255.255.250:3702 asking
for anything of type ``NetworkVideoTransmitter``; matching devices reply as
unicast, straight back to the socket the probe came from. Two things make
that harder than a single ``sendto``:

* **Every interface, not just the default route.** A probe sent from only the
  machine's primary adapter never reaches a camera on a second NIC, a VLAN, or
  a dedicated PoE segment behind a switch that does not bridge multicast
  across ports -- which is the ordinary topology for this product's audience,
  not an edge case. So a socket is opened, bound and probed once per local
  IPv4 address that is actually up.
* **Never block the event loop.** Collecting replies means listening for
  several seconds; doing that with a blocking ``recvfrom`` would stall every
  other request Sentinel is serving for the whole scan. An
  ``asyncio.DatagramProtocol`` on a non-blocking socket collects responses
  without blocking anything.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import psutil

from app.discovery import xmlsafe
from app.logging_config import get_logger

logger = get_logger("DISCOVERY")

MULTICAST_GROUP = "239.255.255.250"
MULTICAST_PORT = 3702

DEFAULT_TIMEOUT_SECONDS = 4.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 15.0

_PROBE_MESSAGE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header>
<w:MessageID>uuid:{message_id}</w:MessageID>
<w:To e:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
</e:Header>
<e:Body>
<d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
</e:Body>
</e:Envelope>"""


def build_probe_message(message_id: str) -> bytes:
    return _PROBE_MESSAGE.format(message_id=message_id).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ProbeMatch:
    """One device's reply, before anything is done with it.

    ``endpoint`` is the device's WS-Addressing endpoint reference (usually
    ``urn:uuid:...``) -- deduplication keys on this, not on address, because
    the same device commonly answers once per local interface it is reachable
    from and would otherwise be counted many times over.
    """

    endpoint: str
    xaddrs: tuple[str, ...]
    scopes: tuple[str, ...]

    @property
    def uuid(self) -> str | None:
        if not self.endpoint:
            return None
        return self.endpoint.rsplit(":", 1)[-1] or None

    @property
    def scope_values(self) -> dict[str, str]:
        """Decode the ``onvif://www.onvif.org/<category>/<value>`` scopes.

        Scopes are the only place WS-Discovery carries anything descriptive
        (manufacturer, model, name); anything that is not in this shape --
        a vendor-private scope namespace, for instance -- is silently
        skipped rather than guessed at.
        """
        values: dict[str, str] = {}
        for scope in self.scopes:
            parts = urlsplit(scope)
            if parts.scheme != "onvif" or parts.netloc != "www.onvif.org":
                continue
            path = parts.path.strip("/")
            category, _, rest = path.partition("/")
            if category and rest:
                values.setdefault(category, unquote(rest))
        return values

    @property
    def hardware(self) -> str | None:
        return self.scope_values.get("hardware")

    @property
    def name(self) -> str | None:
        return self.scope_values.get("name")

    @property
    def manufacturer(self) -> str | None:
        # Not a formally standardised scope category, but enough vendors
        # advertise it that it is worth reading if present; absent means
        # absent, not a guess derived from the hardware string.
        return self.scope_values.get("manufacturer") or self.scope_values.get("vendor")


def parse_probe_matches(data: bytes) -> list[ProbeMatch]:
    """Parse a ``ProbeMatches`` SOAP response.

    Raises :class:`xmlsafe.UnsafeXmlError` for anything malformed or
    oversized; callers are expected to log and skip rather than let one bad
    reply from one device abort a whole scan.
    """
    root = xmlsafe.parse_bounded(data)
    matches: list[ProbeMatch] = []
    for match_el in xmlsafe.find_all(root, "ProbeMatch"):
        address_el = xmlsafe.find_first(match_el, "Address")
        endpoint = (address_el.text or "").strip() if address_el is not None else ""

        xaddrs_el = xmlsafe.find_first(match_el, "XAddrs")
        # XAddrs is a whitespace-separated list of URIs -- a device with more
        # than one network address (or more than one service) puts all of
        # them in this one element.
        xaddrs = tuple((xaddrs_el.text or "").split()) if xaddrs_el is not None else ()

        scopes_el = xmlsafe.find_first(match_el, "Scopes")
        scopes = tuple((scopes_el.text or "").split()) if scopes_el is not None else ()

        if not endpoint and not xaddrs:
            continue  # nothing usable in this ProbeMatch; do not fabricate a device
        matches.append(ProbeMatch(endpoint=endpoint, xaddrs=xaddrs, scopes=scopes))
    return matches


def list_probe_sources() -> list[str]:
    """Every local IPv4 address on an interface that is up.

    Loopback is excluded: a probe sent from 127.0.0.1 cannot reach anything
    outside the machine, and Windows in particular does not support multicast
    on the loopback adapter at all.
    """
    sources: list[str] = []
    stats = psutil.net_if_stats()
    for name, entries in psutil.net_if_addrs().items():
        interface_stats = stats.get(name)
        if interface_stats is None or not interface_stats.isup:
            continue
        for entry in entries:
            if entry.family == socket.AF_INET and not entry.address.startswith("127."):
                sources.append(entry.address)
    return sources


def _build_send_socket(source_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((source_ip, 0))
    # Belt and suspenders alongside the bind: some stacks pick the outgoing
    # interface for a multicast send from IP_MULTICAST_IF rather than from
    # the socket's bound source address.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source_ip))
    # Hop limit of 1: discovery answers "what's on this network segment", not
    # "what's reachable through any router or VPN tunnel this machine has a
    # route for". A public IP on the box is not scanned by this at all --
    # multicast with a TTL of 1 never leaves the local segment regardless.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setblocking(False)
    return sock


class _CollectorProtocol(asyncio.DatagramProtocol):
    """Collects every datagram it receives; nothing more."""

    def __init__(self) -> None:
        self.datagrams: list[bytes] = []

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        del addr  # the source address is not trusted for anything; only the payload matters
        self.datagrams.append(data)

    def error_received(self, exc: Exception) -> None:
        logger.debug("Discovery socket reported an error", extra={"error": str(exc)})


async def send_and_collect(
    sock: socket.socket,
    destination: tuple[str, int],
    payload: bytes,
    *,
    timeout_seconds: float,
) -> list[bytes]:
    """Send ``payload`` to ``destination`` on ``sock`` and collect every
    datagram that arrives within ``timeout_seconds``.

    Split out from :func:`probe_from_source` so it can be exercised directly
    against a real local UDP server in tests, unicast, without depending on
    the test environment supporting outbound multicast.
    """
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(_CollectorProtocol, sock=sock)
    try:
        transport.sendto(payload, destination)
        await asyncio.sleep(timeout_seconds)
    finally:
        transport.close()
    return list(protocol.datagrams)


async def probe_from_source(
    source_ip: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> list[ProbeMatch]:
    """Send one Probe from ``source_ip`` and return every device that answered."""
    message_id = str(uuid.uuid4())
    payload = build_probe_message(message_id)

    try:
        sock = _build_send_socket(source_ip)
    except OSError as exc:
        logger.debug(
            "Could not open a discovery socket on this interface",
            extra={"source": source_ip, "error": str(exc)},
        )
        return []

    try:
        raw_responses = await send_and_collect(
            sock, (MULTICAST_GROUP, MULTICAST_PORT), payload, timeout_seconds=timeout_seconds
        )
    except OSError as exc:
        logger.debug(
            "Discovery probe failed on this interface",
            extra={"source": source_ip, "error": str(exc)},
        )
        return []

    matches: list[ProbeMatch] = []
    for raw in raw_responses:
        try:
            matches.extend(parse_probe_matches(raw))
        except xmlsafe.UnsafeXmlError as exc:
            logger.warning(
                "Ignoring an unparsable WS-Discovery reply",
                extra={"source": source_ip, "error": str(exc)},
            )
    return matches


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    devices: list[ProbeMatch]
    interfaces_probed: list[str]


def _merge(existing: ProbeMatch, incoming: ProbeMatch) -> ProbeMatch:
    """Combine two replies believed to be the same device.

    A multi-homed camera (or an NVR with a channel behind two NICs) may only
    answer some of the interfaces it was probed from, and each reply can list
    different scopes if firmware reports them inconsistently across network
    paths -- keeping the union of everything seen is strictly more useful
    than keeping only the first reply.
    """
    return ProbeMatch(
        endpoint=existing.endpoint or incoming.endpoint,
        xaddrs=tuple(dict.fromkeys((*existing.xaddrs, *incoming.xaddrs))),
        scopes=tuple(dict.fromkeys((*existing.scopes, *incoming.scopes))),
    )


async def discover(*, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> DiscoveryResult:
    """Probe from every usable interface and return the deduplicated devices."""
    timeout_seconds = min(max(timeout_seconds, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
    sources = list_probe_sources()
    if not sources:
        logger.warning("No usable network interface was found for WS-Discovery")
        return DiscoveryResult(devices=[], interfaces_probed=[])

    outcomes = await asyncio.gather(
        *(probe_from_source(source, timeout_seconds=timeout_seconds) for source in sources),
        return_exceptions=True,
    )

    by_key: dict[str, ProbeMatch] = {}
    for source, outcome in zip(sources, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning(
                "Discovery failed on one interface",
                extra={"source": source, "error": str(outcome)},
            )
            continue
        for match in outcome:
            key = match.uuid or match.endpoint or "|".join(match.xaddrs)
            if not key:
                continue
            by_key[key] = _merge(by_key[key], match) if key in by_key else match

    return DiscoveryResult(devices=list(by_key.values()), interfaces_probed=sources)
