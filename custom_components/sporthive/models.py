"""Parsing and aggregation of Sporthive practice data.

This module has no Home Assistant imports, so it can be unit tested on its own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any

_ACTIVITY_ID_RE = re.compile(r"(?:practice/)?(\d{6,})")


def parse_activity_id(text: str | int | None) -> int | None:
    """Return the activity ID from a Sporthive URL or a bare number."""
    if text is None:
        return None
    match = _ACTIVITY_ID_RE.search(str(text).strip())
    return int(match.group(1)) if match else None


def clean_token(token: str) -> str:
    """Strip whitespace and an optional 'Bearer ' prefix from a pasted token."""
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def parse_duration(value: str | float | int | None) -> float | None:
    """Convert '40.762', '1:40.333' or '1:02:03.456' to seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    negative = value.startswith("-")
    parts = value.lstrip("-").split(":")
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    except ValueError:
        return None
    return -seconds if negative else seconds


def _kph(speed: dict[str, Any] | None) -> float | None:
    if not speed or speed.get("kph") is None:
        return None
    return round(float(speed["kph"]), 3)


def related_activity_ids(activity: dict[str, Any]) -> list[int]:
    """Return the activity IDs listed in an activity response (including itself)."""
    ids: list[int] = []
    if activity.get("id") is not None:
        ids.append(int(activity["id"]))
    dto = activity.get("practiceSessionsDto") or {}
    for item in dto.get("locations") or []:
        if item.get("id") is not None and int(item["id"]) not in ids:
            ids.append(int(item["id"]))
    return ids


def summarize_activity(activity: dict[str, Any], sessions: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, JSON-serialisable summary of one activity.

    Personal fields in the API response (chip label, user and account IDs)
    are deliberately not copied.
    """
    location = activity.get("location") or {}
    stats = sessions.get("stats") or {}
    session_list = sessions.get("sessions") or []

    track_length = location.get("trackLength")
    if not track_length:
        sections = location.get("sections") or sessions.get("sections") or []
        track_length = sum(s.get("length") or 0 for s in sections) or None

    laps = [lap for session in session_list for lap in session.get("laps") or []]
    lap_count = stats.get("lapCount")
    if lap_count is None:
        lap_count = len(laps)

    distance_km = round(lap_count * track_length / 1000, 3) if track_length else None

    best = sessions.get("bestLap") or {}
    best_lap_s = parse_duration(stats.get("fastestTime")) or parse_duration(best.get("duration"))

    # When were the best lap and the fastest lap driven?
    timed = [
        (parse_duration(lap.get("duration")), _kph(lap.get("speed")), lap.get("dateTimeStart"))
        for lap in laps
    ]
    best_lap_at = min(
        (t for t in timed if t[0] is not None), key=lambda t: t[0], default=(None, None, None)
    )[2]
    fastest_at = max(
        (t for t in timed if t[1] is not None), key=lambda t: t[1], default=(None, None, None)
    )[2]

    return {
        "id": int(activity["id"]),
        "start": activity.get("startTime"),
        "end": activity.get("endTime"),
        "location": location.get("name"),
        "location_id": location.get("id"),
        "sport": location.get("sport"),
        "country": location.get("country"),
        "track_length_m": track_length,
        "session_count": len(session_list),
        "lap_count": lap_count,
        "distance_km": distance_km,
        "best_lap_s": best_lap_s,
        "best_lap_speed_kph": _kph(best.get("speed")),
        "best_lap_at": best_lap_at or activity.get("startTime"),
        "fastest_speed_at": fastest_at or activity.get("startTime"),
        "average_lap_s": parse_duration(stats.get("averageTime")),
        "median_lap_s": parse_duration(stats.get("medianTime")),
        "total_time_s": parse_duration(stats.get("totalTrainingTime")),
        "active_time_s": parse_duration(stats.get("activeTrainingTime")),
        "average_speed_kph": _kph(stats.get("averageSpeed")),
        "fastest_speed_kph": _kph(stats.get("fastestSpeed")),
    }


def lap_times(sessions: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a compact list of laps for the latest-activity attribute."""
    result: list[dict[str, Any]] = []
    for session in sessions.get("sessions") or []:
        for lap in session.get("laps") or []:
            result.append(
                {
                    "session": session.get("id"),
                    "lap": lap.get("nr"),
                    "start": lap.get("dateTimeStart"),
                    "duration_s": parse_duration(lap.get("duration")),
                    "speed_kph": _kph(lap.get("speed")),
                }
            )
    return result


def activity_start(summary: dict[str, Any]) -> datetime:
    """Return the activity start as an aware UTC datetime."""
    return datetime.fromisoformat(summary["start"]).astimezone(UTC)


# --- Long-term statistics -------------------------------------------------


@dataclass(frozen=True)
class StatDef:
    """Definition of one external long-term statistic."""

    key: str
    name: str
    unit: str | None
    kind: str  # "mean" or "sum"
    value: Callable[[dict[str, Any]], float | None]


STAT_DEFS: tuple[StatDef, ...] = (
    StatDef("best_lap", "Best lap", "s", "mean", lambda a: a.get("best_lap_s")),
    StatDef("average_lap", "Average lap", "s", "mean", lambda a: a.get("average_lap_s")),
    StatDef("median_lap", "Median lap", "s", "mean", lambda a: a.get("median_lap_s")),
    StatDef("fastest_speed", "Fastest speed", "km/h", "mean", lambda a: a.get("fastest_speed_kph")),
    StatDef("laps", "Laps", None, "sum", lambda a: a.get("lap_count")),
    StatDef("distance", "Distance", "km", "sum", lambda a: a.get("distance_km")),
    StatDef(
        "active_time",
        "Active training time",
        "min",
        "sum",
        lambda a: None if a.get("active_time_s") is None else round(a["active_time_s"] / 60, 2),
    ),
)


@dataclass(frozen=True)
class StatSeries:
    """One external statistic with its rows."""

    statistic_id: str
    name: str
    unit: str | None
    kind: str
    rows: list[dict[str, Any]]


_SPORT_ICONS = {
    "iceskating": "mdi:skate",
    "inlineskating": "mdi:roller-skate",
    "cycling": "mdi:bike",
    "running": "mdi:run",
    "athletics": "mdi:run",
    "karting": "mdi:go-kart",
    "motorsport": "mdi:racing-helmet",
}


def sport_name(sport: str | None) -> str | None:
    """Turn 'IceSkating' into 'Ice skating'."""
    if not sport:
        return None
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", sport).split()
    return " ".join([words[0].capitalize(), *(w.lower() for w in words[1:])])


def sport_icon(sport: str | None) -> str:
    """Return a Material Design icon for a sport."""
    return _SPORT_ICONS.get((sport or "").lower().replace(" ", ""), "mdi:trophy-variant-outline")


def location_label(summary: dict[str, Any]) -> str:
    """Return e.g. 'IJsbaan Haarlem Scoreboard (IceSkating)'."""
    name = summary.get("location") or f"Location {summary.get('location_id')}"
    return f"{name} ({summary['sport']})" if summary.get("sport") else name


def statistic_series(
    domain: str, chip_code: str, summaries: Iterable[dict[str, Any]]
) -> list[StatSeries]:
    """Build all statistics: per location (all values) and overall totals (sums).

    Lap times and speeds are only comparable on the same track, so they are
    kept per location. Laps, distance and training time also get an overall
    total across locations.
    """
    summaries = list(summaries)
    slug = chip_slug(chip_code)
    series: list[StatSeries] = []

    totals = build_statistic_rows(summaries)
    for definition in STAT_DEFS:
        if definition.kind == "sum":
            series.append(
                StatSeries(
                    f"{domain}:{slug}_{definition.key}",
                    f"Sporthive {chip_code} {definition.name}",
                    definition.unit,
                    definition.kind,
                    totals[definition.key],
                )
            )

    by_location: dict[Any, list[dict[str, Any]]] = {}
    for summary in summaries:
        by_location.setdefault(summary.get("location_id") or 0, []).append(summary)
    for location_id, items in sorted(by_location.items(), key=lambda kv: str(kv[0])):
        label = location_label(items[-1])
        rows = build_statistic_rows(items)
        for definition in STAT_DEFS:
            series.append(
                StatSeries(
                    f"{domain}:{slug}_loc{location_id}_{definition.key}",
                    f"Sporthive {chip_code} {label} {definition.name}",
                    definition.unit,
                    definition.kind,
                    rows[definition.key],
                )
            )
    return series


def legacy_statistic_ids(domain: str, chip_code: str) -> list[str]:
    """Combined per-chip IDs used by v0.1–v0.2 for lap times and speeds."""
    slug = chip_slug(chip_code)
    return [f"{domain}:{slug}_{d.key}" for d in STAT_DEFS if d.kind == "mean"]


def location_overview(summaries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate per location: personal best, fastest speed, distance, count."""
    result: dict[str, dict[str, Any]] = {}
    for summary in sorted(summaries, key=activity_start):
        key = str(summary.get("location_id") or 0)
        loc = result.setdefault(
            key,
            {
                "location_id": summary.get("location_id"),
                "name": summary.get("location"),
                "label": location_label(summary),
                "sport": summary.get("sport"),
                "track_length_m": summary.get("track_length_m"),
                "activities": 0,
                "laps": 0,
                "distance_km": 0.0,
                "personal_best_s": None,
                "personal_best_activity": None,
                "fastest_speed_kph": None,
                "last_activity": None,
            },
        )
        loc["activities"] += 1
        loc["laps"] += summary.get("lap_count") or 0
        loc["distance_km"] = round(loc["distance_km"] + (summary.get("distance_km") or 0), 3)
        loc["last_activity"] = summary.get("start")
        best = summary.get("best_lap_s")
        if best is not None and (loc["personal_best_s"] is None or best < loc["personal_best_s"]):
            loc["personal_best_s"] = best
            loc["personal_best_activity"] = summary.get("best_lap_at") or summary.get("start")
        speed = summary.get("fastest_speed_kph")
        if speed is not None and (loc["fastest_speed_kph"] is None or speed > loc["fastest_speed_kph"]):
            loc["fastest_speed_kph"] = speed
    return result


def overall_records(summaries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """All-time best lap and fastest speed, with where and when they were set.

    Note that lap times of tracks with different lengths are not comparable;
    the track length is included so this is visible.
    """
    summaries = list(summaries)

    def _record(value_key: str, at_key: str, pick) -> dict[str, Any] | None:
        candidates = [a for a in summaries if a.get(value_key) is not None]
        if not candidates:
            return None
        best = pick(candidates, key=lambda a: a[value_key])
        return {
            "value": best[value_key],
            "achieved_at": best.get(at_key) or best.get("start"),
            "activity_id": best.get("id"),
            "activity_start": best.get("start"),
            "location": best.get("location"),
            "sport": best.get("sport"),
            "track_length_m": best.get("track_length_m"),
        }

    return {
        "best_lap": _record("best_lap_s", "best_lap_at", min),
        "fastest_speed": _record("fastest_speed_kph", "fastest_speed_at", max),
    }


def records_by_sport(
    summaries: Iterable[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any] | None]]:
    """Overall records per sport, e.g. {'IceSkating': {'best_lap': ..., 'fastest_speed': ...}}.

    Lap times differ too much between sports (inline tracks often time half
    laps) to compare them, so each sport has its own records.
    """
    by_sport: dict[str, list[dict[str, Any]]] = {}
    for summary in summaries:
        by_sport.setdefault(summary.get("sport") or "Unknown", []).append(summary)
    return {sport: overall_records(items) for sport, items in by_sport.items()}


def sport_slug(sport: str) -> str:
    """Turn 'IceSkating' into 'ice_skating'."""
    return re.sub(r"[^a-z0-9]+", "_", (sport_name(sport) or "unknown").lower()).strip("_")


def chip_slug(chip_code: str) -> str:
    """Turn 'KX-12345' into 'kx_12345' for use in a statistic ID."""
    return re.sub(r"[^a-z0-9]+", "_", chip_code.lower()).strip("_") or "chip"


def build_statistic_rows(
    summaries: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Build hourly statistic rows per statistic key.

    Each activity becomes one row in the hour it started. Activities that
    start in the same hour are merged. Sum statistics carry a running total
    over the full history, so the result can be re-imported (upserted) as a
    whole at any time.
    """
    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for summary in summaries:
        if not summary.get("start"):
            continue
        hour = activity_start(summary).replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(summary)

    rows: dict[str, list[dict[str, Any]]] = {d.key: [] for d in STAT_DEFS}
    for definition in STAT_DEFS:
        running = 0.0
        for hour in sorted(buckets):
            values = [v for v in (definition.value(a) for a in buckets[hour]) if v is not None]
            if not values:
                continue
            if definition.kind == "mean":
                rows[definition.key].append(
                    {
                        "start": hour,
                        "mean": round(sum(values) / len(values), 3),
                        "min": min(values),
                        "max": max(values),
                    }
                )
            else:
                state = float(sum(values))
                running += state
                rows[definition.key].append(
                    {"start": hour, "state": round(state, 3), "sum": round(running, 3)}
                )
    return rows
