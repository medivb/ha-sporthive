"""Import Sporthive history as external long-term statistics."""

from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .models import StatSeries, legacy_statistic_ids, statistic_series

try:  # Home Assistant 2025.4 and later
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # pragma: no cover - older Home Assistant
    StatisticMeanType = None  # type: ignore[assignment,misc]

_LOGGER = logging.getLogger(__name__)
_META_KEYS = set(getattr(StatisticMetaData, "__annotations__", {}))


def _metadata(series: StatSeries) -> StatisticMetaData:
    is_mean = series.kind == "mean"
    meta: dict[str, Any] = {
        "source": DOMAIN,
        "statistic_id": series.statistic_id,
        "name": series.name,
        "unit_of_measurement": series.unit,
        "has_sum": not is_mean,
    }
    # The metadata schema changed over Home Assistant releases; fill in
    # whichever fields this version expects.
    if "mean_type" in _META_KEYS and StatisticMeanType is not None:
        meta["mean_type"] = StatisticMeanType.ARITHMETIC if is_mean else StatisticMeanType.NONE
    else:
        meta["has_mean"] = is_mean
    if "unit_class" in _META_KEYS:
        meta["unit_class"] = None
    return meta  # type: ignore[return-value]


@callback
def async_import_statistics(
    hass: HomeAssistant,
    chip_code: str,
    summaries: Iterable[dict[str, Any]],
    previous_ids: Iterable[str] = (),
) -> list[str]:
    """Replace all Sporthive statistics of this chip with the given history.

    Existing series are cleared first, so excluded activities, renamed
    locations and the old combined series disappear. The recorder runs the
    clear and import tasks in the order they are queued.
    Returns the statistic IDs that were written.
    """
    series = [s for s in statistic_series(DOMAIN, chip_code, summaries) if s.rows]
    current = [s.statistic_id for s in series]
    to_clear = sorted(set(previous_ids) | set(current) | set(legacy_statistic_ids(DOMAIN, chip_code)))
    get_instance(hass).async_clear_statistics(to_clear)
    for item in series:
        data = [StatisticData(**row) for row in item.rows]  # type: ignore[typeddict-item]
        async_add_external_statistics(hass, _metadata(item), data)
    _LOGGER.debug("Imported %s Sporthive statistics for %s", len(series), chip_code)
    return current
