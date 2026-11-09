"""Data update coordinator for Sporthive."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SporthiveAuthError, SporthiveClient, SporthiveError, SporthiveNotFoundError
from .const import (
    BACKFILL_INTERVAL,
    CONF_ACCOUNT_ID,
    CONF_CHIP,
    CONF_EXCLUDED,
    CONF_SEED_ACTIVITY,
    DOMAIN,
    INITIAL_ACTIVITY_COUNT,
    MAX_NEW_ACTIVITIES_PER_UPDATE,
    RECENT_ACTIVITY_COUNT,
    REQUEST_DELAY_SECONDS,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
)
from .models import (
    activity_start,
    lap_times,
    location_overview,
    records_by_sport,
    related_activity_ids,
    summarize_activity,
)
from .statistics import async_import_statistics

_LOGGER = logging.getLogger(__name__)


@dataclass
class SporthiveData:
    """Data exposed to the entities."""

    latest: dict[str, Any] | None
    latest_laps: list[dict[str, Any]] = field(default_factory=list)
    activity_count: int = 0
    pending_count: int = 0
    excluded_count: int = 0
    account_activity_count: int | None = None
    locations: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per sport code: {"best_lap": record | None, "fastest_speed": record | None}
    records: dict[str, dict[str, dict[str, Any] | None]] = field(default_factory=dict)


class SporthiveCoordinator(DataUpdateCoordinator[SporthiveData]):
    """Discover activities, keep a local history and feed long-term statistics."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SporthiveClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.chip_code: str = entry.data[CONF_CHIP]
        self._seed_id: int = entry.data[CONF_SEED_ACTIVITY]
        self._excluded: set[str] = {str(i) for i in entry.options.get(CONF_EXCLUDED, [])}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._state: dict[str, Any] = {}
        # Re-import statistics once after every (re)load, e.g. after the
        # exclusions changed or after an upgrade.
        self._needs_import = True

    async def async_load(self) -> None:
        """Load the locally stored history."""
        stored = await self._store.async_load() or {}
        self._state = {
            "activities": stored.get("activities", {}),
            "pending": stored.get("pending", []),
            "latest_laps": stored.get("latest_laps", []),
            "latest_laps_id": stored.get("latest_laps_id"),
            "listed_all": stored.get("listed_all", False),
            "account_activity_count": stored.get("account_activity_count"),
            "statistic_ids": stored.get("statistic_ids", []),
        }
        # Activities stored by older versions lack the time of the best and
        # fastest lap; fetch those once more.
        for key, summary in self._state["activities"].items():
            if "best_lap_at" not in summary and key not in self._excluded:
                if int(key) not in self._state["pending"]:
                    self._state["pending"].append(int(key))

    # -- helpers -----------------------------------------------------------

    @property
    def excluded(self) -> set[str]:
        """Activity IDs excluded via the options."""
        return self._excluded

    @property
    def all_activities(self) -> dict[str, dict[str, Any]]:
        """All stored activities, including excluded ones (for the options form)."""
        return self._state["activities"]

    def _included(self) -> list[dict[str, Any]]:
        return [a for k, a in self.all_activities.items() if k not in self._excluded]

    def _latest_summary(self) -> dict[str, Any] | None:
        included = self._included()
        return max(included, key=activity_start) if included else None

    def _queue(self, activity_ids: list[int]) -> None:
        pending: list[int] = self._state["pending"]
        for activity_id in activity_ids:
            key = str(activity_id)
            if key not in self.all_activities and key not in self._excluded and activity_id not in pending:
                pending.append(activity_id)

    async def _pause(self) -> None:
        await asyncio.sleep(REQUEST_DELAY_SECONDS)

    @property
    def account_id(self) -> str | None:
        """The MYLAPS account ID (used for the activity list and profile picture)."""
        return self.config_entry.data.get(CONF_ACCOUNT_ID)

    @property
    def _account_id(self) -> str | None:
        return self.config_entry.data.get(CONF_ACCOUNT_ID)

    # -- discovery ---------------------------------------------------------

    async def _ensure_account_id(self) -> None:
        """Entries created before v0.3 do not know the account ID yet."""
        if self._account_id:
            return
        activity = await self.client.async_get_activity(self._seed_id)
        account_id = activity.get("userId") or activity.get("gaUId")
        if account_id:
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_ACCOUNT_ID: account_id}
            )

    async def _discover_from_account(self) -> bool:
        """Queue activities from the account list. Returns False if unavailable."""
        account_id = self._account_id
        if not account_id:
            return False
        count = RECENT_ACTIVITY_COUNT if self._state["listed_all"] else INITIAL_ACTIVITY_COUNT
        try:
            listing = await self.client.async_get_account_activities(account_id, count)
            total = listing.get("activityCount")
            if not self._state["listed_all"] and isinstance(total, int) and total > count:
                await self._pause()
                listing = await self.client.async_get_account_activities(account_id, total)
        except SporthiveNotFoundError:
            _LOGGER.warning("Sporthive account list not available; using activity links instead")
            return False

        ids = [int(a["id"]) for a in listing.get("activities") or [] if a.get("id") is not None]
        self._queue(ids)
        self._state["account_activity_count"] = listing.get("activityCount")
        if not self._state["listed_all"]:
            self._state["listed_all"] = True
            _LOGGER.debug(
                "Sporthive account lists %s activities (activityCount %s)",
                len(ids),
                listing.get("activityCount"),
            )
        return True

    async def _discover_from_links(self) -> None:
        """Fallback: follow the activity links in the newest activity."""
        latest = self._latest_summary()
        probe_id = latest["id"] if latest else self._seed_id
        probe = await self.client.async_get_activity(probe_id)
        self._queue(related_activity_ids(probe))

    # -- update ------------------------------------------------------------

    async def _async_update_data(self) -> SporthiveData:
        new_found = 0
        changed = False
        try:
            await self._ensure_account_id()
            if not await self._discover_from_account():
                await self._discover_from_links()
            changed = True  # listing state may have changed

            # Fetch a limited batch of unknown activities per update.
            processed = 0
            while self._state["pending"] and processed < MAX_NEW_ACTIVITIES_PER_UPDATE:
                activity_id = self._state["pending"][0]
                await self._pause()
                try:
                    activity = await self.client.async_get_activity(activity_id)
                    await self._pause()
                    sessions = await self.client.async_get_sessions(activity_id)
                except SporthiveNotFoundError:
                    _LOGGER.warning("Sporthive activity %s not found; skipping", activity_id)
                    self._state["pending"].pop(0)
                    continue

                summary = summarize_activity(activity, sessions)
                self.all_activities[str(activity_id)] = summary
                self._state["pending"].pop(0)
                processed += 1
                new_found += 1

                newest = self._latest_summary()
                if newest and newest["id"] == summary["id"]:
                    self._state["latest_laps"] = lap_times(sessions)
                    self._state["latest_laps_id"] = summary["id"]

        except SporthiveAuthError as err:
            raise ConfigEntryAuthFailed("Signing in to Sporthive failed") from err
        except SporthiveError as err:
            raise UpdateFailed(f"Error communicating with Sporthive: {err}") from err
        finally:
            if changed or new_found:
                await self._store.async_save(self._state)

        latest = self._latest_summary()
        if latest is None or self._state.get("latest_laps_id") != latest["id"]:
            # The newest activity was excluded; its lap list no longer applies.
            laps: list[dict[str, Any]] = []
        else:
            laps = self._state["latest_laps"]

        if new_found or self._needs_import:
            self._state["statistic_ids"] = async_import_statistics(
                self.hass, self.chip_code, self._included(), self._state["statistic_ids"]
            )
            self._needs_import = False
            await self._store.async_save(self._state)

        _LOGGER.debug(
            "Sporthive: %s new, %s stored, %s excluded, %s pending",
            new_found,
            len(self.all_activities),
            len(self._excluded & set(self.all_activities)),
            len(self._state["pending"]),
        )

        # Poll faster while the history back-fill is still running.
        self.update_interval = BACKFILL_INTERVAL if self._state["pending"] else UPDATE_INTERVAL

        return SporthiveData(
            latest=latest,
            latest_laps=laps,
            activity_count=len(self._included()),
            pending_count=len(self._state["pending"]),
            excluded_count=len(self._excluded & set(self.all_activities)),
            account_activity_count=self._state.get("account_activity_count"),
            locations=location_overview(self._included()),
            records=records_by_sport(self._included()),
        )
