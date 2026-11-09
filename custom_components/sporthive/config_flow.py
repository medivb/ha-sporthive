"""Config flow for Salto KS."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import SaltoKSApiError, SaltoKSAuthError, SaltoKSClient
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_SCAN_INTERVAL,
    CONF_SCOPE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCOPE,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

AUTH_ERROR_KEYS = {
    "invalid_grant": "invalid_auth",
    "invalid_client": "invalid_client",
    "unauthorized_client": "unauthorized_client",
    "invalid_scope": "invalid_scope",
}

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
            vol.Required(CONF_CLIENT_ID, default=defaults.get(CONF_CLIENT_ID, "")): str,
            vol.Optional(CONF_CLIENT_SECRET, default=""): PASSWORD_SELECTOR,
            vol.Optional(CONF_SCOPE, default=defaults.get(CONF_SCOPE, DEFAULT_SCOPE)): str,
        }
    )


class SaltoKSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Salto KS."""

    VERSION = 1
    MINOR_VERSION = 2

    _server_message: str = ""

    async def _validate(self, data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        self._server_message = ""
        client = SaltoKSClient(
            async_get_clientsession(self.hass),
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            data[CONF_CLIENT_ID],
            data.get(CONF_CLIENT_SECRET, ""),
            data.get(CONF_SCOPE, DEFAULT_SCOPE),
        )
        try:
            await client.async_authenticate()
            me = await client.async_get_me()
        except SaltoKSAuthError as err:
            self._server_message = f"{err.error}: {err.description}" if err.description else err.error
            return {}, {"base": AUTH_ERROR_KEYS.get(err.error, "invalid_auth")}
        except SaltoKSApiError as err:
            self._server_message = str(err)
            return {}, {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during Salto KS validation")
            return {}, {"base": "unknown"}
        return me, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            me, errors = await self._validate(user_input)
            if not errors:
                unique = str(me.get("id") or user_input[CONF_USERNAME]).lower()
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                title = str(me.get("email") or user_input[CONF_USERNAME])
                return self.async_create_entry(title=f"Salto KS ({title})", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
            errors=errors,
            description_placeholders={"server_message": self._server_message or "-"},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            _, errors = await self._validate(data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
            description_placeholders={"server_message": self._server_message or "-"},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> SaltoKSOptionsFlow:
        return SaltoKSOptionsFlow()


class SaltoKSOptionsFlow(OptionsFlow):
    """Options: polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    )
                }
            ),
        )
