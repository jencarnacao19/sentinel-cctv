"""A device cannot steer Sentinel's ONVIF requests to another host.

The Media service address comes from the device's own GetCapabilities reply.
Before this was pinned, a device on the network could answer with
``http://127.0.0.1:1984/api/exit`` and Sentinel would POST there, stopping
go2rtc and with it every live view and recording.
"""

from __future__ import annotations

import httpx
import pytest

from app.discovery import onvif, service

DEVICE_XADDR = "http://192.168.50.50:2020/onvif/device_service"


def _capabilities(media_xaddr: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        "<s:Body><tds:GetCapabilitiesResponse><tds:Capabilities>"
        f"<tt:Media><tt:XAddr>{media_xaddr}</tt:XAddr></tt:Media>"
        "</tds:Capabilities></tds:GetCapabilitiesResponse></s:Body></s:Envelope>"
    ).encode()


async def _media_xaddr_for(advertised: str) -> tuple[str, list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=_capabilities(advertised))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await onvif.get_media_xaddr(client, DEVICE_XADDR, None, None)
    return result, requested


class TestMediaServicePinning:
    async def test_an_address_on_another_host_is_moved_back_to_the_device(self) -> None:
        result, requested = await _media_xaddr_for("http://127.0.0.1:1984/api/exit")
        assert result == "http://192.168.50.50:1984/api/exit"
        assert requested == [DEVICE_XADDR]

    async def test_the_advertised_port_and_path_on_the_same_host_are_kept(self) -> None:
        result, _ = await _media_xaddr_for("http://192.168.50.50:8899/onvif/media_service")
        assert result == "http://192.168.50.50:8899/onvif/media_service"

    @pytest.mark.parametrize(
        "advertised", ["file:///etc/passwd", "ftp://192.168.50.50/x", "not a url", "http://h:99999/"]
    )
    async def test_an_unusable_address_falls_back_to_the_device_address(
        self, advertised: str
    ) -> None:
        result, _ = await _media_xaddr_for(advertised)
        assert result == DEVICE_XADDR

    def test_ipv6_hosts_are_bracketed(self) -> None:
        assert (
            onvif.pin_service_address("http://10.0.0.1:80/onvif/media", "fd00::5")
            == "http://[fd00::5]:80/onvif/media"
        )


class TestDiscoveredAddresses:
    async def test_a_discovery_reply_pointing_at_loopback_is_never_contacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def must_not_run(**kwargs: object) -> list[onvif.MediaProfile]:
            raise AssertionError(f"fetch_streams was called with {kwargs}")

        monkeypatch.setattr(onvif, "fetch_streams", must_not_run)
        with pytest.raises(service.DiscoveryError, match="this machine"):
            await service.fetch_onvif_profiles(
                xaddr="http://127.0.0.1:1984/api/exit",
                username=None,
                password=None,
                timeout_seconds=5,
            )
