"""Bounded XML parsing.

Every document here comes from something Sentinel does not control -- a
WS-Discovery multicast reply, an ONVIF SOAP response -- so the parser has to
refuse an attack before it ever builds a tree, not just cope gracefully with
one. These tests exercise the real ``xml.etree`` pull-parser wrapped in
``parse_bounded``, not a mock of it.
"""

from __future__ import annotations

import pytest

from app.discovery import xmlsafe

# A real (trimmed) WS-Discovery ProbeMatches envelope, structurally identical
# to what a consumer IP camera sends back for a NetworkVideoTransmitter probe.
SINGLE_XADDR_ENVELOPE = b"""<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
              xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
              xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
              xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <env:Header>
    <wsa:MessageID>urn:uuid:aaaaaaaa-1111-2222-3333-444444444444</wsa:MessageID>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>
  </env:Header>
  <env:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:1a2b3c4d-0001-0002-0003-000000000001</wsa:Address>
        </wsa:EndpointReference>
        <d:Types>dn:NetworkVideoTransmitter</d:Types>
        <d:Scopes>onvif://www.onvif.org/hardware/IPC-Model-X1 onvif://www.onvif.org/name/Front%20Door</d:Scopes>
        <d:XAddrs>http://192.168.50.64/onvif/device_service</d:XAddrs>
        <d:MetadataVersion>1</d:MetadataVersion>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </env:Body>
</env:Envelope>"""

# A device with two network paths (or two independent services) reports both
# XAddrs whitespace-separated in the one element.
MULTI_XADDR_ENVELOPE = b"""<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
              xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
              xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <env:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <wsa:EndpointReference><wsa:Address>urn:uuid:multi-nic-0001</wsa:Address></wsa:EndpointReference>
        <d:Scopes>onvif://www.onvif.org/hardware/NVR-8CH onvif://www.onvif.org/manufacturer/ExampleCorp</d:Scopes>
        <d:XAddrs>http://192.168.50.10/onvif/device_service http://10.10.0.10/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </env:Body>
</env:Envelope>"""

# Truncated mid-element -- the kind of reply a flaky Wi-Fi camera or a switch
# that drops the tail of a UDP datagram actually produces.
MALFORMED_ENVELOPE = b"""<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope">
  <env:Body>
    <d:ProbeMatches xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
      <d:ProbeMatch>
        <d:XAddrs>http://192.168.50.64/onvif/device_service</d:XAddrs>
"""


class TestParseBounded:
    def test_parses_a_real_probe_matches_envelope(self) -> None:
        root = xmlsafe.parse_bounded(SINGLE_XADDR_ENVELOPE)
        xaddrs = xmlsafe.find_first(root, "XAddrs")
        assert xaddrs is not None
        assert xaddrs.text == "http://192.168.50.64/onvif/device_service"

    def test_multiple_xaddrs_are_preserved_as_one_field(self) -> None:
        root = xmlsafe.parse_bounded(MULTI_XADDR_ENVELOPE)
        xaddrs = xmlsafe.find_first(root, "XAddrs")
        assert xaddrs is not None
        assert xaddrs.text is not None
        assert xaddrs.text.split() == [
            "http://192.168.50.10/onvif/device_service",
            "http://10.10.0.10/onvif/device_service",
        ]

    def test_truncated_document_is_refused(self) -> None:
        with pytest.raises(xmlsafe.UnsafeXmlError, match="not well-formed"):
            xmlsafe.parse_bounded(MALFORMED_ENVELOPE)

    def test_oversized_document_is_refused_before_parsing(self) -> None:
        huge = b"<a>" + b"<b/>" * 100_000 + b"</a>"
        assert len(huge) > xmlsafe.MAX_RESPONSE_BYTES
        with pytest.raises(xmlsafe.UnsafeXmlError, match="byte limit"):
            xmlsafe.parse_bounded(huge)

    def test_a_doctype_is_refused_even_when_well_formed(self) -> None:
        """The billion-laughs attack needs nothing but a DOCTYPE with internal
        entity definitions; refusing any DOCTYPE closes the whole class."""
        bomb = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
            b"<lolz>&lol2;</lolz>"
        )
        with pytest.raises(xmlsafe.UnsafeXmlError, match="DOCTYPE"):
            xmlsafe.parse_bounded(bomb)

    def test_excessive_nesting_is_refused(self) -> None:
        depth = xmlsafe.MAX_ELEMENT_DEPTH + 10
        nested = b"<a>" * depth + b"x" + b"</a>" * depth
        with pytest.raises(xmlsafe.UnsafeXmlError, match="nests"):
            xmlsafe.parse_bounded(nested)

    def test_nesting_at_the_limit_is_accepted(self) -> None:
        depth = xmlsafe.MAX_ELEMENT_DEPTH
        nested = b"<a>" * depth + b"x" + b"</a>" * depth
        root = xmlsafe.parse_bounded(nested)
        assert root.tag == "a"

    def test_empty_document_is_refused(self) -> None:
        with pytest.raises(xmlsafe.UnsafeXmlError):
            xmlsafe.parse_bounded(b"")

    def test_custom_limits_are_honoured(self) -> None:
        with pytest.raises(xmlsafe.UnsafeXmlError):
            xmlsafe.parse_bounded(b"<a>0123456789</a>", max_bytes=5)


class TestLocalName:
    def test_strips_a_namespace_prefix(self) -> None:
        assert xmlsafe.local_name("{http://example.com/ns}Address") == "Address"

    def test_leaves_a_bare_tag_alone(self) -> None:
        assert xmlsafe.local_name("Address") == "Address"


class TestFindHelpers:
    def test_find_first_tolerates_none(self) -> None:
        assert xmlsafe.find_first(None, "Anything") is None

    def test_find_all_tolerates_none(self) -> None:
        assert xmlsafe.find_all(None, "Anything") == []

    def test_find_first_is_namespace_agnostic(self) -> None:
        root = xmlsafe.parse_bounded(SINGLE_XADDR_ENVELOPE)
        # No namespace prefix given here, unlike the document's own "d:" and
        # "wsa:" prefixes -- this is the point of matching by local name.
        assert xmlsafe.find_first(root, "MetadataVersion") is not None

    def test_find_all_returns_every_match_regardless_of_depth(self) -> None:
        root = xmlsafe.parse_bounded(SINGLE_XADDR_ENVELOPE)
        assert len(xmlsafe.find_all(root, "ProbeMatch")) == 1
