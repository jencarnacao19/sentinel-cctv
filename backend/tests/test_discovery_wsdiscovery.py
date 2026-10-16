"""ONVIF WS-Discovery: probe construction, ``ProbeMatches`` parsing, and the
non-blocking send/collect path against a real local UDP server.

``send_and_collect`` is deliberately split out from :func:`probe_from_source`
(see its docstring) precisely so it can be driven unicast, against a real
socket this module stands up, without depending on the sandbox supporting
outbound multicast -- which a CI container or a locked-down dev box routinely
does not. The interface-selection and multicast-send machinery around it
(``list_probe_sources``, ``_build_send_socket``) either works or the OS
refuses it; there is nothing for a test to usefully assert about that beyond
what ``list_probe_sources`` does with whatever ``psutil`` reports, which is
exercised below with synthetic interface data.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from app.discovery import wsdiscovery, xmlsafe
from app.discovery.wsdiscovery import DEFAULT_TIMEOUT_SECONDS, ProbeMatch

# A real (trimmed) WS-Discovery ProbeMatches envelope, structurally identical
# to what a consumer IP camera sends back for a NetworkVideoTransmitter probe.
SINGLE_XADDR_REPLY = b"""<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
              xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
              xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
              xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <env:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:1a2b3c4d-0001-0002-0003-000000000001</wsa:Address>
        </wsa:EndpointReference>
        <d:Types>dn:NetworkVideoTransmitter</d:Types>
        <d:Scopes>onvif://www.onvif.org/hardware/IPC-Model-X1 onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/manufacturer/ExampleCorp</d:Scopes>
        <d:XAddrs>http://192.168.1.64/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </env:Body>
</env:Envelope>"""

# A device with two network paths (or two independent services) reports both
# XAddrs whitespace-separated in the one element.
MULTI_XADDR_REPLY = b"""<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
              xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
              xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <env:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <wsa:EndpointReference><wsa:Address>urn:uuid:multi-nic-0001</wsa:Address></wsa:EndpointReference>
        <d:Scopes>onvif://www.onvif.org/hardware/NVR-8CH onvif://www.onvif.org/manufacturer/ExampleCorp</d:Scopes>
        <d:XAddrs>http://192.168.1.10/onvif/device_service http://10.10.0.10/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </env:Body>
</env:Envelope>"""

# Two independent devices answering the same probe (a normal multi-camera LAN).
TWO_DEVICE_REPLY = b"""<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
              xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
              xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <env:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <wsa:EndpointReference><wsa:Address>urn:uuid:device-one</wsa:Address></wsa:EndpointReference>
        <d:Scopes>onvif://www.onvif.org/hardware/CamOne</d:Scopes>
        <d:XAddrs>http://192.168.1.20/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
      <d:ProbeMatch>
        <wsa:EndpointReference><wsa:Address>urn:uuid:device-two</wsa:Address></wsa:EndpointReference>
        <d:Scopes>onvif://www.onvif.org/hardware/CamTwo</d:Scopes>
        <d:XAddrs>http://192.168.1.21/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </env:Body>
</env:Envelope>"""

# A ProbeMatch with neither an Address nor any XAddrs -- nothing usable, and
# must not be fabricated into a phantom device.
EMPTY_MATCH_REPLY = b"""<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
              xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <env:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <d:Scopes>onvif://www.onvif.org/hardware/Nothing</d:Scopes>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </env:Body>
</env:Envelope>"""

# Truncated mid-element -- the kind of reply a flaky Wi-Fi camera or a switch
# that drops the tail of a UDP datagram actually produces.
MALFORMED_REPLY = b"""<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope">
  <env:Body>
    <d:ProbeMatches xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
      <d:ProbeMatch>
        <d:XAddrs>http://192.168.1.64/onvif/device_service</d:XAddrs>
"""


class TestBuildProbeMessage:
    def test_message_id_is_embedded(self) -> None:
        payload = wsdiscovery.build_probe_message("test-id-123")
        assert b"uuid:test-id-123" in payload

    def test_targets_network_video_transmitter(self) -> None:
        payload = wsdiscovery.build_probe_message("x")
        assert b"dn:NetworkVideoTransmitter" in payload

    def test_is_well_formed_bounded_xml(self) -> None:
        payload = wsdiscovery.build_probe_message("x")
        root = xmlsafe.parse_bounded(payload)
        assert xmlsafe.find_first(root, "Probe") is not None


class TestParseProbeMatches:
    def test_single_device_single_xaddr(self) -> None:
        matches = wsdiscovery.parse_probe_matches(SINGLE_XADDR_REPLY)
        assert len(matches) == 1
        match = matches[0]
        assert match.uuid == "1a2b3c4d-0001-0002-0003-000000000001"
        assert match.xaddrs == ("http://192.168.1.64/onvif/device_service",)
        assert match.hardware == "IPC-Model-X1"
        assert match.name == "Front Door"  # percent-decoded from "Front%20Door"
        assert match.manufacturer == "ExampleCorp"

    def test_multiple_xaddrs_on_one_device_are_kept_as_one_field(self) -> None:
        matches = wsdiscovery.parse_probe_matches(MULTI_XADDR_REPLY)
        assert len(matches) == 1
        assert matches[0].xaddrs == (
            "http://192.168.1.10/onvif/device_service",
            "http://10.10.0.10/onvif/device_service",
        )

    def test_two_devices_in_one_reply_are_both_returned(self) -> None:
        matches = wsdiscovery.parse_probe_matches(TWO_DEVICE_REPLY)
        assert {m.uuid for m in matches} == {"device-one", "device-two"}

    def test_a_match_with_nothing_usable_is_skipped_not_fabricated(self) -> None:
        assert wsdiscovery.parse_probe_matches(EMPTY_MATCH_REPLY) == []

    def test_malformed_reply_raises_unsafe_xml_error(self) -> None:
        with pytest.raises(xmlsafe.UnsafeXmlError):
            wsdiscovery.parse_probe_matches(MALFORMED_REPLY)

    def test_a_non_onvif_scope_scheme_is_ignored(self) -> None:
        reply = b"""<?xml version="1.0"?>
        <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
                    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
          <e:Body><d:ProbeMatches><d:ProbeMatch>
            <d:XAddrs>http://192.168.1.5/onvif/device_service</d:XAddrs>
            <d:Scopes>vendor-private://acme.example/foo/bar</d:Scopes>
          </d:ProbeMatch></d:ProbeMatches></e:Body>
        </e:Envelope>"""
        match = wsdiscovery.parse_probe_matches(reply)[0]
        assert match.hardware is None
        assert match.name is None
        assert match.manufacturer is None


class TestProbeMatchProperties:
    def test_uuid_is_none_without_an_endpoint(self) -> None:
        assert ProbeMatch(endpoint="", xaddrs=(), scopes=()).uuid is None

    def test_uuid_is_the_trailing_segment_of_the_endpoint(self) -> None:
        match = ProbeMatch(endpoint="urn:uuid:abc-123", xaddrs=(), scopes=())
        assert match.uuid == "abc-123"

    def test_scope_properties_are_none_when_no_matching_category_is_present(self) -> None:
        match = ProbeMatch(
            endpoint="urn:uuid:x",
            xaddrs=(),
            scopes=("onvif://www.onvif.org/type/video_encoder",),
        )
        assert match.hardware is None
        assert match.name is None
        assert match.manufacturer is None

    def test_vendor_scope_is_read_as_manufacturer_too(self) -> None:
        match = ProbeMatch(
            endpoint="urn:uuid:x", xaddrs=(), scopes=("onvif://www.onvif.org/vendor/Acme",)
        )
        assert match.manufacturer == "Acme"


class TestListProbeSources:
    def test_only_up_non_loopback_ipv4_addresses_are_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Synthesises what ``psutil`` reports for a machine with three
        interfaces: one usable adapter, one that is down, and loopback --
        exactly the mix that made this filtering necessary in the first
        place."""

        class _Stats:
            def __init__(self, isup: bool) -> None:
                self.isup = isup

        class _Addr:
            def __init__(self, family: socket.AddressFamily, address: str) -> None:
                self.family = family
                self.address = address

        stats = {"eth0": _Stats(True), "eth1": _Stats(False), "lo": _Stats(True)}
        addrs = {
            "eth0": [
                _Addr(socket.AF_INET, "192.168.1.5"),
                _Addr(socket.AF_INET6, "fe80::1"),
            ],
            "eth1": [_Addr(socket.AF_INET, "10.0.0.9")],  # down: must be excluded
            "lo": [_Addr(socket.AF_INET, "127.0.0.1")],  # loopback: must be excluded
        }
        monkeypatch.setattr(wsdiscovery.psutil, "net_if_stats", lambda: stats)
        monkeypatch.setattr(wsdiscovery.psutil, "net_if_addrs", lambda: addrs)

        assert wsdiscovery.list_probe_sources() == ["192.168.1.5"]

    def test_no_interfaces_gives_an_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsdiscovery.psutil, "net_if_stats", lambda: {})
        monkeypatch.setattr(wsdiscovery.psutil, "net_if_addrs", lambda: {})
        assert wsdiscovery.list_probe_sources() == []


def _open_udp_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    return sock


class TestSendAndCollect:
    """Exercised against a real UDP socket on loopback -- unicast, as the
    function's own docstring says it is meant to be tested, so this needs no
    multicast support from the sandbox running the suite."""

    async def test_receives_a_real_reply_over_loopback(self) -> None:
        loop = asyncio.get_running_loop()
        device_sock = _open_udp_socket()
        device_addr = device_sock.getsockname()

        async def fake_camera() -> None:
            data, addr = await loop.sock_recvfrom(device_sock, 4096)
            assert b"NetworkVideoTransmitter" in data
            device_sock.sendto(SINGLE_XADDR_REPLY, addr)

        camera_task = asyncio.create_task(fake_camera())
        try:
            client_sock = _open_udp_socket()
            responses = await wsdiscovery.send_and_collect(
                client_sock,
                device_addr,
                wsdiscovery.build_probe_message("test"),
                timeout_seconds=1.0,
            )
        finally:
            await camera_task
            device_sock.close()

        assert len(responses) == 1
        matches = wsdiscovery.parse_probe_matches(responses[0])
        assert matches[0].uuid == "1a2b3c4d-0001-0002-0003-000000000001"

    async def test_multiple_replies_from_the_same_device_are_all_collected(self) -> None:
        loop = asyncio.get_running_loop()
        device_sock = _open_udp_socket()
        device_addr = device_sock.getsockname()

        async def chatty_camera() -> None:
            _, addr = await loop.sock_recvfrom(device_sock, 4096)
            device_sock.sendto(SINGLE_XADDR_REPLY, addr)
            device_sock.sendto(MULTI_XADDR_REPLY, addr)

        task = asyncio.create_task(chatty_camera())
        try:
            client_sock = _open_udp_socket()
            responses = await wsdiscovery.send_and_collect(
                client_sock, device_addr, b"probe", timeout_seconds=1.0
            )
        finally:
            await task
            device_sock.close()

        assert len(responses) == 2

    async def test_no_reply_gives_an_empty_list_rather_than_hanging(self) -> None:
        # Bind-then-close to get a real ephemeral port number with nothing
        # listening on it -- a reliable way to produce "no reply" on loopback
        # without depending on some fixed unused port being free.
        placeholder = _open_udp_socket()
        dead_addr = placeholder.getsockname()
        placeholder.close()

        client_sock = _open_udp_socket()
        loop = asyncio.get_running_loop()
        before = loop.time()
        responses = await wsdiscovery.send_and_collect(
            client_sock, dead_addr, b"probe", timeout_seconds=0.3
        )
        assert responses == []
        assert loop.time() - before < 5.0  # bounded, not an accidental hang


class TestDiscover:
    """``discover`` fans a probe out across every interface and merges
    replies believed to be the same device; both are pure orchestration over
    :func:`list_probe_sources` and :func:`probe_from_source`, so they are
    exercised here with those two seams replaced rather than real sockets --
    the socket-level behaviour underneath is ``TestSendAndCollect``'s job."""

    async def test_a_device_seen_from_two_interfaces_is_merged_not_duplicated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_probe(source: str, *, timeout_seconds: float) -> list[ProbeMatch]:
            del timeout_seconds
            if source == "192.168.1.5":
                return [
                    ProbeMatch(
                        endpoint="urn:uuid:cam-1",
                        xaddrs=("http://192.168.1.64/onvif/device_service",),
                        scopes=("onvif://www.onvif.org/hardware/X1",),
                    )
                ]
            if source == "10.10.0.5":
                return [
                    ProbeMatch(
                        endpoint="urn:uuid:cam-1",
                        xaddrs=("http://10.10.0.64/onvif/device_service",),
                        scopes=(),
                    )
                ]
            return []

        monkeypatch.setattr(wsdiscovery, "list_probe_sources", lambda: ["192.168.1.5", "10.10.0.5"])
        monkeypatch.setattr(wsdiscovery, "probe_from_source", fake_probe)

        result = await wsdiscovery.discover(timeout_seconds=1.0)
        assert len(result.devices) == 1
        device = result.devices[0]
        assert set(device.xaddrs) == {
            "http://192.168.1.64/onvif/device_service",
            "http://10.10.0.64/onvif/device_service",
        }
        assert device.hardware == "X1"  # union of scopes keeps the one real reply carried
        assert result.interfaces_probed == ["192.168.1.5", "10.10.0.5"]

    async def test_no_usable_interface_returns_an_empty_result_without_probing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        async def fake_probe(source: str, *, timeout_seconds: float) -> list[ProbeMatch]:
            nonlocal called
            called = True
            del source, timeout_seconds
            return []

        monkeypatch.setattr(wsdiscovery, "list_probe_sources", lambda: [])
        monkeypatch.setattr(wsdiscovery, "probe_from_source", fake_probe)

        result = await wsdiscovery.discover()
        assert result.devices == []
        assert result.interfaces_probed == []
        assert called is False

    async def test_one_interface_failing_does_not_abort_the_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_probe(source: str, *, timeout_seconds: float) -> list[ProbeMatch]:
            del timeout_seconds
            if source == "bad-if":
                raise OSError("interface vanished mid-scan")
            return [ProbeMatch(endpoint="urn:uuid:ok", xaddrs=("http://192.168.1.9/x",), scopes=())]

        monkeypatch.setattr(wsdiscovery, "list_probe_sources", lambda: ["bad-if", "192.168.1.9"])
        monkeypatch.setattr(wsdiscovery, "probe_from_source", fake_probe)

        result = await wsdiscovery.discover()
        assert len(result.devices) == 1
        assert result.devices[0].uuid == "ok"

    async def test_timeout_is_clamped_to_the_documented_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[float] = []

        async def fake_probe(source: str, *, timeout_seconds: float) -> list[ProbeMatch]:
            del source
            captured.append(timeout_seconds)
            return []

        monkeypatch.setattr(wsdiscovery, "list_probe_sources", lambda: ["192.168.1.5"])
        monkeypatch.setattr(wsdiscovery, "probe_from_source", fake_probe)

        await wsdiscovery.discover(timeout_seconds=999)
        await wsdiscovery.discover(timeout_seconds=0.001)
        await wsdiscovery.discover()

        assert captured == [
            wsdiscovery.MAX_TIMEOUT_SECONDS,
            wsdiscovery.MIN_TIMEOUT_SECONDS,
            DEFAULT_TIMEOUT_SECONDS,
        ]
