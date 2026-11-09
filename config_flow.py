"""Config flow for Sporthive."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.util import dt as dt_util
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import build_auth
from .api import (
    SporthiveAuthError,
    SporthiveClient,
    SporthiveError,
    SporthiveNotFoundError,
    SporthiveTokenRejectedError,
)
from .auth import SporthiveLoginFlowError
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ACTIVITY,
    CONF_EXCLUDED,
    CONF_CHIP,
    CONF_REFRESH_EXPIRES,
    CONF_REFRESH_TOKEN,
    CONF_SEED_ACTIVITY,
    DOMAIN,
)
from .models import activity_start, location_label, parse_activity_id

_LOGGER = logging.getLogger(__name__)

_USERNAME_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
_PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


class SporthiveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sporthive."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SporthiveOptionsFlow()

    async def _validate(
        self, activity_id: int, username: str, password: str
    ) -> tuple[dict[str, Any] | None, dict[str, str], dict[str, Any]]:
        """Sign in and read the activity. Returns (activity, errors, refresh data)."""
        refresh: dict[str, Any] = {}

        def _keep(token: str, expires) -> None:
            refresh[CONF_REFRESH_TOKEN] = token
            refresh[CONF_REFRESH_EXPIRES] = expires.isoformat() if expires else None

        auth = build_auth(self.hass, username, password, on_refresh_token=_keep)
        client = SporthiveClient(async_get_clientsession(self.hass), auth)
        try:
            activity = await client.async_get_activity(activity_id)
        except SporthiveTokenRejectedError:
            return None, {"base": "token_rejected"}, {}
        except SporthiveAuthError:
            return None, {"base": "invalid_auth"}, {}
        except SporthiveNotFoundError:
            return None, {CONF_ACTIVITY: "not_found"}, {}
        except SporthiveLoginFlowError as err:
            _LOGGER.warning("MYLAPS sign-in flow failed: %s", err)
            return None, {"base": "login_flow"}, {}
        except SporthiveError as err:
            _LOGGER.warning("Sporthive setup failed: %s", err)
            return None, {"base": "cannot_connect"}, {}
        except Exception:
            _LOGGER.exception("Unexpected error during Sporthive setup")
            return None, {"base": "unknown"}, {}
        if not activity.get("chipCode"):
            return None, {"base": "no_chip"}, {}
        return activity, {}, refresh

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the MYLAPS credentials and one activity link."""
        errors: dict[str, str] = {}
        if user_input is not None:
            activity_id = parse_activity_id(user_input[CONF_ACTIVITY])
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            if activity_id is None:
                errors[CONF_ACTIVITY] = "invalid_activity"
            else:
                activity, errors, refresh = await self._validate(activity_id, username, password)
                if activity is not None:
                    chip = activity["chipCode"]
                    await self.async_set_unique_id(chip)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Sporthive {chip}",
                        data={
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                            CONF_CHIP: chip,
                            CONF_SEED_ACTIVITY: activity_id,
                            CONF_ACCOUNT_ID: activity.get("userId") or activity.get("gaUId"),
                            **refresh,
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): _USERNAME_SELECTOR,
                vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                vol.Required(CONF_ACTIVITY): str,
            }
        )
        suggested = {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD}
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication when the credentials stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the MYLAPS credentials again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            activity, errors, refresh = await self._validate(
                entry.data[CONF_SEED_ACTIVITY], username, password
            )
            if activity is not None:
                if activity["chipCode"] != entry.data[CONF_CHIP]:
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_USERNAME: username, CONF_PASSWORD: password, **refresh},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): _USERNAME_SELECTOR,
                vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_USERNAME: entry.data.get(CONF_USERNAME, "")}
            ),
            errors=errors,
        )


class SporthiveOptionsFlow(OptionsFlow):
    """Choose activities that should be ignored."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show all known activities; ticked ones are excluded."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_EXCLUDED: sorted(user_input.get(CONF_EXCLUDED, []))}
            )

        coordinator = getattr(self.config_entry, "runtime_data", None)
        activities = coordinator.all_activities.values() if coordinator else []
        options = [
            SelectOptionDict(
                value=str(a["id"]),
                label=(
                    f"{dt_util.as_local(activity_start(a)).strftime('%Y-%m-%d %H:%M')} – "
                    f"{location_label(a)} – {a.get('lap_count')} laps"
                ),
            )
            for a in sorted(activities, key=activity_start)
        ]
        current = [str(i) for i in self.config_entry.options.get(CONF_EXCLUDED, [])]
        known = {o["value"] for o in options}
        options += [SelectOptionDict(value=i, label=f"Activity {i}") for i in current if i not in known]

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_EXCLUDED, default=current): SelectSelector(
                        SelectSelectorConfig(
                            options=options, multiple=True, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )
