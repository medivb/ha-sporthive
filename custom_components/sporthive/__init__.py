"""The Sporthive integration: MYLAPS practice (training) results in Home Assistant."""

from __future__ import annotations

from datetime import datetime

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.storage import Store

from .api import SporthiveClient
from .auth import SporthiveAuth
from .const import CONF_EXCLUDED, CONF_REFRESH_EXPIRES, CONF_REFRESH_TOKEN, DOMAIN, STORAGE_VERSION
from .coordinator import SporthiveCoordinator

PLATFORMS: list[Platform] = [Platform.IMAGE, Platform.SENSOR]

type SporthiveConfigEntry = ConfigEntry[SporthiveCoordinator]


def build_auth(
    hass: HomeAssistant,
    username: str,
    password: str,
    refresh_token: str | None = None,
    refresh_expires: str | None = None,
    on_refresh_token=None,
) -> SporthiveAuth:
    """Create the MYLAPS sign-in helper."""
    return SporthiveAuth(
        session=async_get_clientsession(hass),
        # Separate session for the sign-in pages, detached after each sign-in.
        # Cookies are handled by the auth module (aiohttp drops B2C's cookie names).
        login_session_factory=lambda: async_create_clientsession(
            hass, auto_cleanup=False, cookie_jar=aiohttp.DummyCookieJar()
        ),
        username=username,
        password=password,
        refresh_token=refresh_token,
        refresh_expires=datetime.fromisoformat(refresh_expires) if refresh_expires else None,
        on_refresh_token=on_refresh_token,
    )


async def async_setup_entry(hass: HomeAssistant, entry: SporthiveConfigEntry) -> bool:
    """Set up Sporthive from a config entry."""
    if CONF_USERNAME not in entry.data or CONF_PASSWORD not in entry.data:
        # Entries created by v0.1 (manual token) need credentials.
        raise ConfigEntryAuthFailed("Sign in with your MYLAPS username and password")

    @callback
    def _save_refresh_token(token: str, expires: datetime | None) -> None:
        # Keep the refresh token across restarts, so a restart does not
        # trigger a full sign-in.
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_REFRESH_TOKEN: token,
                CONF_REFRESH_EXPIRES: expires.isoformat() if expires else None,
            },
        )

    auth = build_auth(
        hass,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_REFRESH_TOKEN),
        entry.data.get(CONF_REFRESH_EXPIRES),
        _save_refresh_token,
    )
    client = SporthiveClient(async_get_clientsession(hass), auth)
    coordinator = SporthiveCoordinator(hass, entry, client)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: SporthiveConfigEntry) -> None:
    """Reload when the excluded activities change.

    The listener also fires when the stored refresh token is updated; that
    must not cause a reload.
    """
    excluded = {str(i) for i in entry.options.get(CONF_EXCLUDED, [])}
    if excluded != entry.runtime_data.excluded:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SporthiveConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SporthiveConfigEntry) -> None:
    """Delete the locally stored history when the entry is removed."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
