"""Sensors for the latest Sporthive activity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfLength, UnitOfSpeed, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SporthiveConfigEntry
from .const import ACTIVITY_URL, DOMAIN
from .coordinator import SporthiveCoordinator, SporthiveData
from .models import sport_icon, sport_name, sport_slug


@dataclass(frozen=True, kw_only=True)
class SporthiveSensorDescription(SensorEntityDescription):
    """Describes a Sporthive sensor."""

    value_fn: Callable[[SporthiveData], Any]


def _latest(key: str) -> Callable[[SporthiveData], Any]:
    return lambda data: (data.latest or {}).get(key)


def _start(data: SporthiveData) -> datetime | None:
    if not data.latest or not data.latest.get("start"):
        return None
    return datetime.fromisoformat(data.latest["start"])


SENSORS: tuple[SporthiveSensorDescription, ...] = (
    SporthiveSensorDescription(
        key="last_activity",
        name="Last training",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_start,
    ),
    SporthiveSensorDescription(
        key="location", name="Last training location", icon="mdi:map-marker", value_fn=_latest("location")
    ),
    SporthiveSensorDescription(
        key="sport",
        name="Last training sport",
        value_fn=lambda data: sport_name((data.latest or {}).get("sport")),
    ),
    SporthiveSensorDescription(
        key="laps", name="Last training laps", icon="mdi:counter", value_fn=_latest("lap_count")
    ),
    SporthiveSensorDescription(
        key="distance",
        name="Last training distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=1,
        value_fn=_latest("distance_km"),
    ),
    SporthiveSensorDescription(
        key="best_lap",
        name="Last training best lap",
        icon="mdi:timer-star-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=3,
        value_fn=_latest("best_lap_s"),
    ),
    SporthiveSensorDescription(
        key="average_lap",
        name="Last training average lap",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=3,
        value_fn=_latest("average_lap_s"),
    ),
    SporthiveSensorDescription(
        key="median_lap",
        name="Last training median lap",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=3,
        value_fn=_latest("median_lap_s"),
    ),
    SporthiveSensorDescription(
        key="active_time",
        name="Last training active time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=1,
        value_fn=_latest("active_time_s"),
    ),
    SporthiveSensorDescription(
        key="total_time",
        name="Last training total time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=1,
        value_fn=_latest("total_time_s"),
    ),
    SporthiveSensorDescription(
        key="fastest_speed",
        name="Last training fastest speed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=1,
        value_fn=_latest("fastest_speed_kph"),
    ),
    SporthiveSensorDescription(
        key="average_speed",
        name="Last training average speed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=1,
        value_fn=_latest("average_speed_kph"),
    ),
    SporthiveSensorDescription(
        key="activities",
        name="Activities imported",
        icon="mdi:history",
        value_fn=lambda data: data.activity_count,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SporthiveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sporthive sensors."""
    coordinator = entry.runtime_data
    async_add_entities(SporthiveSensor(coordinator, description) for description in SENSORS)

    # Per-location sensors, added when a new location shows up.
    known: set[str] = set()

    @callback
    def _add_locations() -> None:
        new = [key for key in coordinator.data.locations if key not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(
            SporthiveLocationSensor(coordinator, key, description)
            for key in new
            for description in LOCATION_SENSORS
        )

    _add_locations()
    entry.async_on_unload(coordinator.async_add_listener(_add_locations))

    # Overall records per sport, added when a new sport shows up.
    known_sports: set[str] = set()

    @callback
    def _add_sports() -> None:
        new = [sport for sport in coordinator.data.records if sport not in known_sports]
        if not new:
            return
        known_sports.update(new)
        async_add_entities(
            SporthiveSportRecordSensor(coordinator, sport, description)
            for sport in new
            for description in RECORD_SENSORS
        )

    _add_sports()
    entry.async_on_unload(coordinator.async_add_listener(_add_sports))

    # v0.4 had combined records across sports; remove those entities.
    registry = er.async_get(hass)
    slug = coordinator.chip_code.lower()
    for old_key in ("overall_best_lap", "overall_fastest_speed"):
        if entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"{slug}_{old_key}"):
            registry.async_remove(entity_id)


class SporthiveSensor(CoordinatorEntity[SporthiveCoordinator], SensorEntity):
    """A value from the latest activity."""

    _attr_has_entity_name = True
    # Lap lists can be long; keep them out of the recorder database.
    _unrecorded_attributes = frozenset({"lap_times"})
    entity_description: SporthiveSensorDescription

    def __init__(
        self, coordinator: SporthiveCoordinator, description: SporthiveSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        slug = coordinator.chip_code.lower()
        self._attr_unique_id = f"{slug}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.chip_code)},
            name=f"Sporthive {coordinator.chip_code}",
            manufacturer="MYLAPS",
            model="Transponder",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        key = self.entity_description.key
        latest = data.latest
        if key == "activities":
            return {
                "pending_backfill": data.pending_count,
                "excluded": data.excluded_count,
                "activities_on_account": data.account_activity_count,
            }
        if not latest:
            return None
        # Every latest-activity sensor says where and what it was.
        attrs: dict[str, Any] = {
            "location": latest.get("location"),
            "sport": sport_name(latest.get("sport")),
            "activity_start": latest.get("start"),
        }
        if key == "location":
            attrs.update(
                location_id=latest.get("location_id"),
                country=latest.get("country"),
                track_length_m=latest.get("track_length_m"),
            )
        if key == "sport":
            attrs["sport_code"] = latest.get("sport")
        if key == "best_lap":
            attrs["achieved_at"] = latest.get("best_lap_at")
        if key == "fastest_speed":
            attrs["achieved_at"] = latest.get("fastest_speed_at")
        if key == "laps":
            attrs.update({
                "activity_id": latest["id"],
                "url": ACTIVITY_URL.format(activity_id=data.latest["id"]),
                "sessions": data.latest.get("session_count"),
                "track_length_m": data.latest.get("track_length_m"),
                "lap_times": data.latest_laps,
            })
        return attrs

    @property
    def icon(self) -> str | None:
        if self.entity_description.key == "sport":
            return sport_icon((self.coordinator.data.latest or {}).get("sport"))
        return super().icon


@dataclass(frozen=True, kw_only=True)
class SporthiveLocationSensorDescription(SensorEntityDescription):
    """Describes a per-location sensor."""

    value_key: str


LOCATION_SENSORS: tuple[SporthiveLocationSensorDescription, ...] = (
    SporthiveLocationSensorDescription(
        key="personal_best",
        name="personal best lap",
        icon="mdi:trophy-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=3,
        value_key="personal_best_s",
    ),
    SporthiveLocationSensorDescription(
        key="fastest_speed",
        name="fastest speed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=1,
        value_key="fastest_speed_kph",
    ),
    SporthiveLocationSensorDescription(
        key="total_distance",
        name="total distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=1,
        value_key="distance_km",
    ),
    SporthiveLocationSensorDescription(
        key="activities",
        name="activities",
        icon="mdi:history",
        value_key="activities",
    ),
)


class SporthiveLocationSensor(CoordinatorEntity[SporthiveCoordinator], SensorEntity):
    """All-time value for one location, e.g. the personal best lap."""

    _attr_has_entity_name = True
    entity_description: SporthiveLocationSensorDescription

    def __init__(
        self,
        coordinator: SporthiveCoordinator,
        location_key: str,
        description: SporthiveLocationSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._location_key = location_key
        location = coordinator.data.locations[location_key]
        slug = coordinator.chip_code.lower()
        self._attr_unique_id = f"{slug}_loc{location_key}_{description.key}"
        self._attr_name = f"{location['name'] or 'Location ' + location_key} {description.name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.chip_code)},
        )

    @property
    def _location(self) -> dict[str, Any] | None:
        return self.coordinator.data.locations.get(self._location_key)

    @property
    def available(self) -> bool:
        return super().available and self._location is not None

    @property
    def native_value(self) -> Any:
        location = self._location
        return location.get(self.entity_description.value_key) if location else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        location = self._location
        if not location:
            return None
        attrs: dict[str, Any] = {
            "sport": sport_name(location.get("sport")),
            "track_length_m": location.get("track_length_m"),
            "last_activity": location.get("last_activity"),
        }
        if self.entity_description.key == "personal_best":
            attrs["set_on"] = location.get("personal_best_activity")
        if self.entity_description.key == "activities":
            attrs["laps"] = location.get("laps")
        return attrs


RECORD_SENSORS: tuple[SporthiveLocationSensorDescription, ...] = (
    SporthiveLocationSensorDescription(
        key="best_lap",
        name="Overall best lap",
        icon="mdi:trophy",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=3,
        value_key="best_lap",
    ),
    SporthiveLocationSensorDescription(
        key="fastest_speed",
        name="Overall fastest speed",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=1,
        value_key="fastest_speed",
    ),
)


class SporthiveSportRecordSensor(CoordinatorEntity[SporthiveCoordinator], SensorEntity):
    """All-time best lap or fastest speed for one sport."""

    _attr_has_entity_name = True
    entity_description: SporthiveLocationSensorDescription

    def __init__(
        self,
        coordinator: SporthiveCoordinator,
        sport: str,
        description: SporthiveLocationSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._sport = sport
        slug = coordinator.chip_code.lower()
        self._attr_unique_id = f"{slug}_overall_{description.key}_{sport_slug(sport)}"
        # e.g. "Overall best lap ice skating" -> sensor.<device>_overall_best_lap_ice_skating
        self._attr_name = f"{description.name} {(sport_name(sport) or sport).lower()}"
        self._attr_icon = sport_icon(sport) if description.key == "best_lap" else description.icon
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.chip_code)})

    @property
    def _record(self) -> dict[str, Any] | None:
        records = self.coordinator.data.records.get(self._sport) or {}
        return records.get(self.entity_description.value_key)

    @property
    def available(self) -> bool:
        return super().available and self._sport in self.coordinator.data.records

    @property
    def native_value(self) -> Any:
        record = self._record
        return record["value"] if record else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        record = self._record
        if not record:
            return None
        return {
            "sport": sport_name(self._sport),
            "achieved_at": record["achieved_at"],
            "location": record["location"],
            "track_length_m": record["track_length_m"],
            "activity_start": record["activity_start"],
            "url": ACTIVITY_URL.format(activity_id=record["activity_id"]),
        }
