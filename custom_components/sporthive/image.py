"""Image entities for Salto KS.

- The account profile picture (``image_url`` from /me) on the account device.
- Every other picture URL field (for example ``owner.image_url`` of a site)
  as an image entity on the device the data belongs to.
"""

from __future__ import annotations

import logging
import mimetypes
from typing import Any
from urllib.parse import urlparse

from homeassistant.components.image import Image, ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import (
    KIND_IMAGE,
    SCOPE_LOCK,
    SCOPE_ME,
    MetadataKey,
    SaltoKSEntity,
    SaltoKSMetadataEntity,
    humanize,
    lock_name,
    setup_metadata_platform,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Handled by SaltoKSProfileImage (kept for a stable entity ID).
PROFILE_IMAGE_KEY = MetadataKey(SCOPE_ME, ("image_url",))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaltoKSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    coordinator.claimed_metadata.add(PROFILE_IMAGE_KEY.uid)
    async_add_entities([SaltoKSProfileImage(hass, coordinator)])
    entry.async_on_unload(
        setup_metadata_platform(
            coordinator, KIND_IMAGE, SaltoKSMetadataImage, async_add_entities
        )
    )


def detect_image_type(content: bytes, header: str | None, url: str) -> str | None:
    """Determine the image MIME type.

    Picture URLs from Salto KS point to Azure Blob Storage, which serves them as
    application/octet-stream. The type is therefore taken from the header only
    when it is an image type, and otherwise from the file content or extension.
    """
    if header and header.split(";", 1)[0].strip().lower().startswith("image/"):
        return header.split(";", 1)[0].strip().lower()
    head = content[:512]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if b"<svg" in head.lower():
        return "image/svg+xml"
    guessed, _ = mimetypes.guess_type(urlparse(url).path)
    if guessed and guessed.startswith("image/"):
        return guessed
    return None


class _UrlImageMixin:
    """Serves the image at a URL taken from coordinator data.

    Must be listed first in the bases so its coordinator hook runs.
    """

    _current_url: str | None = None

    def _source_url(self) -> str | None:
        raise NotImplementedError

    def _sync_url(self) -> None:
        url = self._source_url() or None
        if url != self._current_url:
            self._current_url = url
            self._attr_image_url = url
            self._cached_image = None
            self._attr_image_last_updated = dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_url()
        super()._handle_coordinator_update()  # type: ignore[misc]

    async def _async_load_image_from_url(self, url: str) -> Image | None:
        """Load the picture, accepting a generic content type from blob storage."""
        response = await self._fetch_url(url)  # type: ignore[attr-defined]
        if response is None:
            return None
        content_type = detect_image_type(
            response.content, response.headers.get("content-type"), url
        )
        if content_type is None:
            _LOGGER.warning(
                "%s: picture is not a recognised image format",
                self.entity_id,  # type: ignore[attr-defined]
            )
            return None
        return Image(content_type=content_type, content=response.content)

    async def async_image(self) -> bytes | None:
        if not self._current_url:
            return None
        return await super().async_image()  # type: ignore[misc]


class SaltoKSProfileImage(_UrlImageMixin, SaltoKSEntity, ImageEntity):
    """The account's profile picture."""

    _attr_translation_key = "profile_image"

    def __init__(self, hass: HomeAssistant, coordinator: SaltoKSCoordinator) -> None:
        SaltoKSEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_profile_image"
        self._sync_url()

    def _source_url(self) -> str | None:
        return (self.coordinator.data.me if self.coordinator.data else {}).get("image_url")

    @property
    def available(self) -> bool:
        return super().available and self._current_url is not None


class SaltoKSMetadataImage(_UrlImageMixin, SaltoKSMetadataEntity, ImageEntity):
    """A picture URL field of a site or lock (for example the site owner's picture)."""

    def __init__(self, coordinator: SaltoKSCoordinator, key: MetadataKey) -> None:
        SaltoKSMetadataEntity.__init__(self, coordinator, key)
        ImageEntity.__init__(self, coordinator.hass)
        self._attr_entity_category = None
        self._sync_url()

    def _source_url(self) -> str | None:
        found, value = self.raw_value
        return value if found and isinstance(value, str) else None

    @property
    def name(self) -> str:
        parent = self.key.path[:-1]
        label = f"{humanize(parent)} picture" if parent else "Picture"
        if self.key.scope == SCOPE_LOCK:
            return f"{lock_name(self.lock_data, self.key.lock_id or '')} {label.lower()}"
        return label

    @property
    def available(self) -> bool:
        return super().available and self._current_url is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return {"source_field": ".".join(self.key.path)}
