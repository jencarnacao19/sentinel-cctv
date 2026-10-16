"""The config flow: manual entry, zeroconf discovery, reauth and options."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.sentinel.const import CONF_TOKEN, CONF_URL, DOMAIN

from .conftest import BASE_URL, HANDSHAKE_PAYLOAD, SERVER_ID, TOKEN, register_sentinel

# Same two-branch import as config_flow.py; see the comment there.
try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # pragma: no cover
    from homeassistant.components.zeroconf import (  # type: ignore[no-redef]
        ZeroconfServiceInfo,
    )


def zeroconf_info() -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address="192.0.2.10",
        ip_addresses=["192.0.2.10"],
        hostname="sentinel.local.",
        name="sentinel._sentinel._tcp.local.",
        port=8090,
        type="_sentinel._tcp.local.",
        properties={"version": "0.1.0", "id": SERVER_ID},
    )


async def test_manual_entry_creates_the_entry(
    hass: HomeAssistant,
    sentinel_api: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This is a config-flow test; entity/platform setup is covered separately.
    # Avoid starting Home Assistant's camera HTTP worker here, whose process-
    # global safe-shutdown thread outlives the first test that imports it.
    from custom_components import sentinel as sentinel_component

    monkeypatch.setattr(sentinel_component, "PLATFORMS", [])

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_TOKEN: TOKEN}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == TOKEN
    assert result["result"].unique_id == SERVER_ID

    # The token has to travel as a header, never a query string: query strings
    # end up in access logs and proxy logs in a way headers do not.
    headers = sentinel_api.mock_calls[0][3] or {}
    assert headers.get("Authorization") == f"Bearer {TOKEN}"

    # This flow sets the entry up immediately. Unload it so Home Assistant's
    # strict test harness can verify that no HTTP shutdown thread survives the
    # test which created it.
    await hass.config_entries.async_unload(result["result"].entry_id)
    await hass.async_block_till_done()


async def test_an_unreachable_server_is_reported_as_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{BASE_URL}/api/v1/integrations/home-assistant", exc=TimeoutError("no route")
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_TOKEN: TOKEN}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_a_rejected_token_is_reported_as_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    register_sentinel(aioclient_mock, handshake_status=401, handshake={"detail": "no"})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_TOKEN: "sctv_wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_a_server_without_the_endpoint_is_a_version_problem_not_a_network_one(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    # A 404 means the server answered perfectly well and has simply never heard
    # of this endpoint. Reporting that as "cannot connect" would send the
    # operator hunting through firewall rules for a version mismatch.
    register_sentinel(
        aioclient_mock, handshake_status=404, handshake={"detail": "Not found."}
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_TOKEN: TOKEN}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported_version"}


async def test_a_future_api_version_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    register_sentinel(
        aioclient_mock, handshake={**HANDSHAKE_PAYLOAD, "api_version": 99}
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_TOKEN: TOKEN}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported_version"}


async def test_the_same_server_cannot_be_added_twice(
    hass: HomeAssistant,
    sentinel_api: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_TOKEN: TOKEN}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_discovery_allows_correcting_a_stale_address(
    hass: HomeAssistant, sentinel_api: AiohttpClientMocker
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_TOKEN: TOKEN}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The discovery address is the default, but can be corrected when a stale
    # mDNS announcement survives a development deployment.
    assert result["data"][CONF_URL] == BASE_URL


async def test_zeroconf_ignores_a_server_already_configured(
    hass: HomeAssistant,
    sentinel_api: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info()
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_replaces_the_token_without_a_second_entry(
    hass: HomeAssistant,
    sentinel_api: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    config_entry.add_to_hass(hass)

    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: "sctv_replacement"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN] == "sctv_replacement"
    # Exactly one entry: reauth must update, never add.
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauth_against_a_different_server_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    # Same address, different install: accepting this would silently repoint an
    # entry -- and every entity id under it -- at somebody else's box.
    config_entry.add_to_hass(hass)
    register_sentinel(
        aioclient_mock,
        handshake={**HANDSHAKE_PAYLOAD, "unique_id": "a-different-install"},
    )

    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: "sctv_replacement"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"


@pytest.mark.parametrize("interval", [15, 60, 3600])
async def test_the_options_flow_accepts_valid_intervals(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, interval: int
) -> None:
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: interval}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SCAN_INTERVAL] == interval


@pytest.mark.parametrize("interval", [1, 99999])
async def test_the_options_flow_refuses_an_interval_outside_its_bounds(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, interval: int
) -> None:
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)

    with pytest.raises(vol.Invalid):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: interval}
        )


def test_every_string_has_a_portuguese_translation() -> None:
    """A translation file that silently lags the English one shows raw keys."""
    root = Path(__file__).resolve().parents[2] / "custom_components" / "sentinel"
    strings: dict[str, Any] = json.loads(
        (root / "strings.json").read_text(encoding="utf-8")
    )
    portuguese: dict[str, Any] = json.loads(
        (root / "translations" / "pt.json").read_text(encoding="utf-8")
    )
    english: dict[str, Any] = json.loads(
        (root / "translations" / "en.json").read_text(encoding="utf-8")
    )

    def leaf_keys(node: Any, prefix: str = "") -> set[str]:
        if not isinstance(node, dict):
            return {prefix}
        keys: set[str] = set()
        for key, value in node.items():
            keys |= leaf_keys(value, f"{prefix}.{key}" if prefix else key)
        return keys

    expected = leaf_keys(strings)
    assert not expected - leaf_keys(portuguese), "pt.json is missing keys"
    assert not expected - leaf_keys(english), "en.json is missing keys"
