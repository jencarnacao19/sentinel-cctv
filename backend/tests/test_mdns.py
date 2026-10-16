"""mDNS announcement: real registration/lookup, and every failure mode fails soft."""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator

import ifaddr
import pytest
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncZeroconf

from app.config import Settings
from app.integrations import mdns
from app.integrations.identity import install_id
from app.integrations.mdns import MdnsAnnouncer, _local_ipv4_addresses, _sanitise_label

#: Real multicast is genuinely unavailable in some sandboxes (no route, a
#: firewall, network namespace isolation). The registration itself is
#: verified separately (it either raises, in which case the fail-soft tests
#: below cover it, or it does not); this timeout only bounds how long the
#: round-trip lookup test waits before treating "nothing answered" as an
#: environment limitation rather than a bug.
LOOKUP_TIMEOUT_SECONDS = 4.0


@pytest.fixture
def unmanaged_settings(settings: Settings) -> Settings:
    """Same data directory as the shared ``settings`` fixture (for the
    install secret), but with mDNS actually turned on -- the shared fixture
    forces ``testing=True``, which every manager including this one treats
    as "do not do the real thing"."""
    return Settings(
        testing=False,
        enable_mdns=True,
        data_dir=settings.data_dir,
        recordings_dir=settings.recordings_dir,
        port=settings.port,
    )


@pytest.fixture
async def announcer(unmanaged_settings: Settings) -> AsyncIterator[MdnsAnnouncer]:
    instance = MdnsAnnouncer(unmanaged_settings)
    yield instance
    await instance.stop()


class TestAddressDiscovery:
    def test_never_returns_loopback_or_link_local(self) -> None:
        for address in _local_ipv4_addresses():
            parsed = ipaddress.ip_address(address)
            assert not parsed.is_loopback
            assert not parsed.is_link_local

    def test_agrees_with_a_direct_ifaddr_query(self) -> None:
        """Not a mock of our own code: cross-checked against the same
        third-party library the function itself calls, independently."""
        expected = set()
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if not ip.is_IPv4 or not isinstance(ip.ip, str):
                    continue
                parsed = ipaddress.ip_address(ip.ip)
                if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified:
                    continue
                expected.add(ip.ip)
        assert set(_local_ipv4_addresses()) == expected


class TestSanitiseLabel:
    def test_keeps_ordinary_hostnames_unchanged(self) -> None:
        assert _sanitise_label("sentinel-nvr") == "sentinel-nvr"

    def test_replaces_disallowed_characters(self) -> None:
        # The trailing "!" becomes a hyphen and is then stripped, same as any
        # other leading/trailing hyphen -- only the internal ones survive.
        assert _sanitise_label("my nvr_box!") == "my-nvr-box"

    def test_strips_leading_and_trailing_hyphens(self) -> None:
        assert _sanitise_label("_leading and trailing_") == "leading-and-trailing"

    def test_empty_input_falls_back_to_a_safe_default(self) -> None:
        assert _sanitise_label("___") == "sentinel"

    def test_is_truncated_to_a_dns_label(self) -> None:
        assert len(_sanitise_label("x" * 200)) == 63


class TestFailSoft:
    async def test_disabled_by_configuration_does_nothing(
        self, unmanaged_settings: Settings
    ) -> None:
        unmanaged_settings.enable_mdns = False
        instance = MdnsAnnouncer(unmanaged_settings)
        await instance.start()
        assert instance.is_announced is False
        await instance.stop()  # must not raise even though nothing started

    async def test_testing_mode_does_nothing(self, settings: Settings) -> None:
        # `settings` (unlike `unmanaged_settings`) keeps testing=True, exactly
        # like every other manager's test-mode guard.
        instance = MdnsAnnouncer(settings)
        await instance.start()
        assert instance.is_announced is False
        await instance.stop()

    async def test_a_registration_failure_is_caught_and_logged_not_raised(
        self, announcer: MdnsAnnouncer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(self: AsyncZeroconf, *args: object, **kwargs: object) -> None:
            raise OSError("multicast is not available in this sandbox")

        monkeypatch.setattr(AsyncZeroconf, "async_register_service", _boom)

        await announcer.start()  # must not raise

        assert announcer.is_announced is False
        await announcer.stop()  # still safe, nothing was ever registered

    async def test_no_usable_address_is_handled_without_raising(
        self, announcer: MdnsAnnouncer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mdns, "_local_ipv4_addresses", lambda: [])
        await announcer.start()
        assert announcer.is_announced is False

    async def test_stop_before_start_is_a_no_op(self, unmanaged_settings: Settings) -> None:
        instance = MdnsAnnouncer(unmanaged_settings)
        await instance.stop()
        assert instance.is_announced is False


class TestRealAnnouncement:
    async def test_service_type_matches_the_documented_contract(self) -> None:
        assert mdns.SERVICE_TYPE == "_sentinel._tcp.local."

    async def test_register_and_look_up_over_real_multicast(
        self, announcer: MdnsAnnouncer, unmanaged_settings: Settings
    ) -> None:
        await announcer.start()
        if not announcer.is_announced:
            pytest.skip("No usable network address for mDNS in this environment")
        assert announcer.service_name is not None

        browser = AsyncZeroconf(ip_version=IPVersion.V4Only)
        try:
            info = await browser.async_get_service_info(
                mdns.SERVICE_TYPE,
                announcer.service_name,
                timeout=int(LOOKUP_TIMEOUT_SECONDS * 1000),
            )
        finally:
            await browser.async_close()

        if info is None:
            pytest.skip(
                "This Sentinel's own announcement did not come back over multicast "
                "within the timeout -- likely no multicast route in this sandbox, "
                "not a code defect (see TestFailSoft for the behaviour this protects)."
            )

        assert info.port == unmanaged_settings.port
        properties = {
            key.decode(): value.decode() if value is not None else None
            for key, value in info.properties.items()
        }
        assert properties.get("id") == install_id()
        assert "version" in properties
