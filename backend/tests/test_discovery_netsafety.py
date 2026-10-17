"""Host validation: discovery may only ever touch private network space.

Every host reaching :func:`ensure_scannable_host` is either something a caller
typed into the API or an address a device volunteered over unauthenticated
multicast -- neither is trustworthy, so this is the one gate every network-
touching function in the package goes through.
"""

from __future__ import annotations

import asyncio

import pytest

from app.discovery import netsafety


class TestLiteralAddresses:
    @pytest.mark.parametrize(
        "address",
        [
            "192.168.50.50",
            "10.0.0.5",
            "172.16.4.4",
            "127.0.0.1",
            "169.254.1.1",  # link-local, the address class an unconfigured camera boots with
            "fd00::1",  # unique local IPv6
            "::1",
        ],
    )
    async def test_private_and_loopback_addresses_are_accepted(self, address: str) -> None:
        await netsafety.ensure_scannable_host(address)  # must not raise

    @pytest.mark.parametrize(
        "address",
        [
            "8.8.8.8",
            "1.1.1.1",
            "203.0.113.5",  # TEST-NET-3, publicly routable address space
            "2001:4860:4860::8888",
        ],
    )
    async def test_public_addresses_are_refused(self, address: str) -> None:
        with pytest.raises(netsafety.UnscannableHostError, match="not a private address"):
            await netsafety.ensure_scannable_host(address)


class TestHostnameResolution:
    async def test_a_hostname_resolving_to_a_private_address_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_getaddrinfo(host: str, port: object) -> list[tuple[object, ...]]:
            del host, port
            return [(None, None, None, None, ("192.168.50.20", 0))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
        await netsafety.ensure_scannable_host("nvr.lan")  # must not raise

    async def test_a_hostname_resolving_to_a_public_address_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_getaddrinfo(host: str, port: object) -> list[tuple[object, ...]]:
            del host, port
            return [(None, None, None, None, ("93.184.216.34", 0))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(netsafety.UnscannableHostError, match="not a private address"):
            await netsafety.ensure_scannable_host("public-decoy.example")

    async def test_an_ipv6_scope_id_is_stripped_before_parsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_getaddrinfo(host: str, port: object) -> list[tuple[object, ...]]:
            del host, port
            return [(None, None, None, None, ("fe80::1%eth0", 0, 0, 0))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
        await netsafety.ensure_scannable_host("link-local-camera")  # must not raise

    async def test_a_name_that_does_not_resolve_is_refused_with_an_actionable_message(
        self,
    ) -> None:
        # ".invalid" is reserved by RFC 2606 to never resolve, so this is
        # deterministic rather than depending on a flaky DNS query.
        with pytest.raises(netsafety.UnscannableHostError, match="ould not resolve"):
            await netsafety.ensure_scannable_host("definitely-not-a-camera.invalid")


class TestSlowResolution:
    async def test_resolution_is_bounded_rather_than_hanging_forever(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def hangs_forever(host: str, port: object) -> list[tuple[object, ...]]:
            del host, port
            await asyncio.sleep(3600)
            return []

        monkeypatch.setattr(netsafety, "RESOLVE_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", hangs_forever)
        with pytest.raises(netsafety.UnscannableHostError, match="ould not resolve"):
            await netsafety.ensure_scannable_host("slow-dns-camera")


class TestOnvifTargets:
    """ONVIF calls are POSTs to an address a device chose; never this machine."""

    @pytest.mark.parametrize("address", ["127.0.0.1", "127.8.9.10", "::1"])
    async def test_loopback_is_refused(self, address: str) -> None:
        with pytest.raises(netsafety.UnscannableHostError, match="this machine"):
            await netsafety.ensure_onvif_host(address)

    async def test_a_hostname_resolving_to_loopback_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_getaddrinfo(host: str, port: object) -> list[tuple[object, ...]]:
            del host, port
            return [(None, None, None, None, ("127.0.0.1", 0))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(netsafety.UnscannableHostError, match="this machine"):
            await netsafety.ensure_onvif_host("camera.attacker.example")

    @pytest.mark.parametrize("address", ["192.168.50.50", "10.0.0.5", "169.254.1.1", "fd00::1"])
    async def test_devices_on_the_network_are_accepted(self, address: str) -> None:
        await netsafety.ensure_onvif_host(address)  # must not raise

    async def test_public_addresses_are_still_refused(self) -> None:
        with pytest.raises(netsafety.UnscannableHostError, match="not a private address"):
            await netsafety.ensure_onvif_host("8.8.8.8")
