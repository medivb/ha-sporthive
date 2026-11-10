"""Profile picture of the MYLAPS account."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import SporthiveConfigEntry
from .api import SporthiveError
from .const import DOMAIN
from .coordinator import SporthiveCoordinator

_LOGGER = logging.getLogger(__name__)
# Re-download the picture at most this often.
_REFRESH = timedelta(hours=24)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SporthiveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the profile picture."""
    coordinator = entry.runtime_data
    if coordinator.account_id:
        async_add_entities([SporthiveProfileImage(hass, coordinator)])


class SporthiveProfileImage(CoordinatorEntity[SporthiveCoordinator], ImageEntity):
    """The picture uploaded to the MYLAPS account."""

    _attr_has_entity_name = True
    _attr_name = "Profile picture"
    _attr_icon = "mdi:account-circle"

    def __init__(self, hass: HomeAssistant, coordinator: SporthiveCoordinator) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{coordinator.chip_code.lower()}_profile_picture"
        # Full device info: the image platform may be set up before the sensor
        # platform, and the device name decides the entity ID.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.chip_code)},
            name=f"Sporthive {coordinator.chip_code}",
            manufacturer="MYLAPS",
            model="Transponder",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._attr_image_last_updated = dt_util.utcnow()
        self._cached: bytes | None = None
        self._fetched_at: datetime | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        # Tell the frontend to fetch the picture again once a day, so a new
        # upload on sporthive.com shows up.
        if self._fetched_at and dt_util.utcnow() - self._fetched_at > _REFRESH:
            self._cached = None
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Return the picture, downloading it when needed."""
        if self._cached is not None:
            return self._cached
        account_id = self.coordinator.account_id
        if not account_id:
            return None
        try:
            result = await self.coordinator.client.async_get_profile_image(account_id)
        except SporthiveError as err:
            _LOGGER.debug("Could not download the Sporthive profile picture: %s", err)
            return None
        if result is None:
            return None
        self._cached, content_type = result
        self._attr_content_type = content_type.split(";")[0].strip() or "image/jpeg"
        self._fetched_at = dt_util.utcnow()
        return self._cached
