"""The Salto KS integration."""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SaltoKSClient
from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_SCOPE, DEFAULT_SCOPE, DOMAIN
from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import account_name, is_id_field, is_image_field, site_name

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.IMAGE,
    Platform.LOCK,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> bool:
    """Set up Salto KS from a config entry."""
    client = SaltoKSClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_CLIENT_ID],
        entry.data.get(CONF_CLIENT_SECRET, ""),
        entry.data.get(CONF_SCOPE, DEFAULT_SCOPE),
    )
    coordinator = SaltoKSCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Create the account device first so site devices can reference it.
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=account_name(coordinator.data.me),
        manufacturer="SALTO Systems",
        model="Salto KS account",
    )

    @callback
    def _sync_device_names() -> None:
        """Keep device names aligned with customer_reference / profile changes."""
        if not coordinator.data:
            return
        wanted = {entry.entry_id: account_name(coordinator.data.me)}
        for site_id, site in coordinator.data.sites.items():
            wanted[f"site_{site_id}"] = site_name(site, site_id)
        for ident, name in wanted.items():
            device = dev_reg.async_get_device(identifiers={(DOMAIN, ident)})
            if device and device.name != name:
                dev_reg.async_update_device(device.id, name=name)

    _remove_image_url_sensors(hass, entry)

    entry.async_on_unload(coordinator.async_add_listener(_sync_device_names))
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _remove_image_url_sensors(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> None:
    """Remove sensors created for picture URL fields by earlier versions;
    these fields are now image entities."""
    ent_reg = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if reg_entry.domain != Platform.SENSOR or "__" not in reg_entry.unique_id:
            continue
        path = tuple(reg_entry.unique_id.rsplit("__", 1)[1].split("."))
        if is_image_field(path):
            ent_reg.async_remove(reg_entry.entity_id)


async def async_migrate_entry(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> bool:
    """Migrate older config entries."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        # 1.2: hide identifier entities created by earlier versions (once only,
        # so a later choice by the user to show them is respected).
        ent_reg = er.async_get(hass)
        for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            if "__" not in reg_entry.unique_id or reg_entry.hidden_by is not None:
                continue
            path = tuple(reg_entry.unique_id.rsplit("__", 1)[1].split("."))
            if is_id_field(path):
                ent_reg.async_update_entity(
                    reg_entry.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
                )
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SaltoKSConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing site devices that are no longer returned by the API."""
    data = entry.runtime_data.data
    for domain, ident in device.identifiers:
        if domain != DOMAIN:
            continue
        if ident == entry.entry_id:
            return False
        if ident.startswith("site_") and ident.removeprefix("site_") in data.sites:
            return False
    return True
