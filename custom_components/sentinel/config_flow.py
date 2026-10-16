"""Config flow: zeroconf discovery, manual entry, reauth, and the polling option.

Every path funnels through :func:`_async_validate`, so "cannot connect",
"invalid auth" and "unsupported server version" are told apart in exactly one
place and every entry point -- typed by a human or found on the network --
reports the same three errors the same way.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

try:  # Home Assistant 2025.2 and later
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # pragma: no cover - exercised only on the older branch
    # Before 2025.2 this class lived in the zeroconf *component*. The manifest
    # supports versions on both sides of that move, and importing the new path
    # unconditionally would make the integration fail to load entirely on the
    # older ones -- a blank "failed to set up" with no useful message.
    from homeassistant.components.zeroconf import (  # type: ignore[no-redef]
        ZeroconfServiceInfo,
    )

from .api import (
    SentinelAuthError,
    SentinelClient,
    SentinelConnectionError,
    SentinelError,
    SentinelUnsupportedVersionError,
    normalise_base_url,
)
from .const import (
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
)
from .models import ServerInfo

_LOGGER = logging.getLogger(__name__)


async def _async_validate(
    hass: Any, url: str, token: str
) -> tuple[ServerInfo | None, str | None]:
    """Try the handshake and map every failure to one translated error key."""
    session = async_get_clientsession(hass)
    client = SentinelClient(session, url, token)
    try:
        info = await client.async_get_server_info()
    except SentinelAuthError:
        return None, "invalid_auth"
    except SentinelUnsupportedVersionError:
        return None, "unsupported_version"
    except SentinelConnectionError:
        return None, "cannot_connect"
    except SentinelError:
        _LOGGER.exception("Unexpected Sentinel API error validating a config entry")
        return None, "unknown"
    except Exception:
        _LOGGER.exception("Unexpected error validating a Sentinel config entry")
        return None, "unknown"
    return info, None


class SentinelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handles zeroconf discovery, manual setup and reauth."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_url: str | None = None
        self._discovered_name: str | None = None

    # --- Manual entry ----------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalise_base_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "cannot_connect"
            else:
                info, error = await _async_validate(
                    self.hass, url, user_input[CONF_TOKEN]
                )
                if error:
                    errors["base"] = error
                elif info is not None:
                    await self.async_set_unique_id(info.unique_id)
                    self._abort_if_unique_id_configured(updates={CONF_URL: url})
                    return self.async_create_entry(
                        title=info.name or DEFAULT_NAME,
                        data={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN]},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_URL,
                        default=user_input.get(CONF_URL, "") if user_input else "",
                    ): str,
                    vol.Required(CONF_TOKEN): str,
                }
            ),
            errors=errors,
        )

    # --- Zeroconf discovery ------------------------------------------------

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        host = discovery_info.host
        port = discovery_info.port or DEFAULT_PORT
        try:
            url = normalise_base_url(f"http://{host}:{port}")
        except ValueError:
            return self.async_abort(reason="cannot_connect")

        existing_urls = {
            entry.data.get(CONF_URL)
            for entry in self._async_current_entries(include_ignore=False)
        }
        if url in existing_urls:
            return self.async_abort(reason="already_configured")

        # Two Sentinel builds announcing while both wait on the same human to
        # type a token would otherwise start two identical flows.
        for progress in self._async_in_progress():
            if progress["context"].get("discovered_url") == url:
                return self.async_abort(reason="already_in_progress")

        raw_name = discovery_info.name or DEFAULT_NAME
        self._discovered_name = raw_name.split("._sentinel", 1)[0] or DEFAULT_NAME
        self._discovered_url = url
        self.context["discovered_url"] = url
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovered_url is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalise_base_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "cannot_connect"
            else:
                info, error = await _async_validate(
                    self.hass, url, user_input[CONF_TOKEN]
                )
                if error:
                    errors["base"] = error
                elif info is not None:
                    await self.async_set_unique_id(info.unique_id)
                    self._abort_if_unique_id_configured(updates={CONF_URL: url})
                    return self.async_create_entry(
                        title=info.name or self._discovered_name or DEFAULT_NAME,
                        data={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN]},
                    )

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_URL,
                        default=(
                            user_input.get(CONF_URL, self._discovered_url)
                            if user_input
                            else self._discovered_url
                        ),
                    ): str,
                    vol.Required(CONF_TOKEN): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "name": self._discovered_name or DEFAULT_NAME,
                "host": self._discovered_url,
            },
        )

    # --- Reauth ------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await _async_validate(
                self.hass, reauth_entry.data[CONF_URL], user_input[CONF_TOKEN]
            )
            if error:
                errors["base"] = error
            elif info is not None:
                if info.unique_id != reauth_entry.unique_id:
                    # A different Sentinel is now answering at this address --
                    # most likely the wrong device got the old DHCP lease. That
                    # is not what reauth is for; the user needs the discovery
                    # or manual flow to add it as what it actually is.
                    return self.async_abort(reason="unique_id_mismatch")
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
            description_placeholders={"name": reauth_entry.title},
        )

    # --- Options -------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SentinelOptionsFlow:
        return SentinelOptionsFlow()


class SentinelOptionsFlow(OptionsFlow):
    """The only setting worth exposing: how often to poll when push is down."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS
        )
        schema = vol.Schema(
            {
                vol.Optional(CONF_SCAN_INTERVAL, default=current): vol.All(
                    vol.Coerce(int),
                    vol.Range(
                        min=MIN_SCAN_INTERVAL_SECONDS, max=MAX_SCAN_INTERVAL_SECONDS
                    ),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
