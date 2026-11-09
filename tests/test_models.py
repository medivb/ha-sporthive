"""Tests for the Home Assistant-independent parsing code."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from custom_components.sporthive.models import (
    build_statistic_rows,
    location_overview,
    sport_icon,
    sport_name,
    statistic_series,
    chip_slug,
    clean_token,
    lap_times,
    parse_activity_id,
    parse_duration,
    related_activity_ids,
    summarize_activity,
)

FIXTURES = Path(__file__).parent / "fixtures"
ACTIVITY = json.loads((FIXTURES / "activity.json").read_text())
SESSIONS = json.loads((FIXTURES / "sessions.json").read_text())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("40.762", 40.762),
        ("1:40.333", 100.333),
        ("48:32.564", 2912.564),
        ("1:02:03.5", 3723.5),
        ("-1:06.312", -66.312),
        ("", None),
        (None, None),
        ("abc", None),
    ],
)
def test_parse_duration(value, expected) -> None:
    result = parse_duration(value)
    assert result == pytest.approx(expected) if expected is not None else result is None


def test_parse_activity_id_and_token() -> None:
    assert parse_activity_id("https://sporthive.com/practice/8582809434/activity") == 8582809434
    assert parse_activity_id(" 8582809434 ") == 8582809434
    assert parse_activity_id("https://sporthive.com/") is None
    assert clean_token("  Bearer abc.def ") == "abc.def"
    assert chip_slug("KX-87154") == "kx_87154"


def test_related_ids() -> None:
    assert related_activity_ids(ACTIVITY) == [8582809434, 8557340454, 8524880271]


def test_summary() -> None:
    summary = summarize_activity(ACTIVITY, SESSIONS)
    assert summary["lap_count"] == 26
    assert summary["distance_km"] == 10.4
    assert summary["best_lap_s"] == pytest.approx(40.762)
    assert summary["average_lap_s"] == pytest.approx(100.333)
    assert summary["active_time_s"] == pytest.approx(2608.669)
    assert summary["session_count"] == 2
    assert summary["fastest_speed_kph"] == pytest.approx(35.327)
    # Personal fields are not copied.
    assert "Test" not in json.dumps(summary)
    assert "chip" not in " ".join(summary)


def test_lap_times() -> None:
    laps = lap_times(SESSIONS)
    assert len(laps) == 4
    assert laps[1] == {
        "session": 1,
        "lap": 2,
        "start": "2026-11-07T17:05:25.399+01:00",
        "duration_s": 40.762,
        "speed_kph": 35.327,
    }


def test_statistic_rows_running_sum_and_merge() -> None:
    first = summarize_activity(ACTIVITY, SESSIONS)
    second = copy.deepcopy(first)
    second.update(id=2, start="2026-11-08T10:15:00+01:00", lap_count=10, distance_km=4.0, best_lap_s=39.0)
    third = copy.deepcopy(second)  # same hour as `second`: merged
    third.update(id=3, start="2026-11-08T10:45:00+01:00", lap_count=5, distance_km=2.0, best_lap_s=41.0)

    rows = build_statistic_rows([second, first, third])

    laps = rows["laps"]
    assert [r["start"] for r in laps] == [
        datetime(2026, 11, 7, 16, tzinfo=UTC),
        datetime(2026, 11, 8, 9, tzinfo=UTC),
    ]
    assert [r["state"] for r in laps] == [26, 15]
    assert [r["sum"] for r in laps] == [26, 41]
    assert rows["distance"][-1]["sum"] == pytest.approx(16.4)

    best = rows["best_lap"][-1]
    assert (best["min"], best["max"], best["mean"]) == (39.0, 41.0, 40.0)


def test_series_per_location_and_totals() -> None:
    rink = summarize_activity(ACTIVITY, SESSIONS)
    other = copy.deepcopy(rink)
    other.update(
        id=9, start="2026-11-08T10:00:00+01:00", location="Velodrome", location_id=77,
        sport="Cycling", lap_count=10, distance_km=2.5, best_lap_s=20.0,
    )
    series = {s.statistic_id: s for s in statistic_series("sporthive", "KX-1", [rink, other])}

    assert "sporthive:kx_1_best_lap" not in series  # no cross-track lap times
    assert series["sporthive:kx_1_loc2822_best_lap"].rows[0]["mean"] == 40.762
    assert series["sporthive:kx_1_loc77_best_lap"].rows[0]["mean"] == 20.0
    assert series["sporthive:kx_1_loc77_best_lap"].name == "Sporthive KX-1 Velodrome (Cycling) Best lap"
    assert series["sporthive:kx_1_laps"].rows[-1]["sum"] == 36
    assert series["sporthive:kx_1_loc2822_laps"].rows[-1]["sum"] == 26

    overview = location_overview([rink, other])
    assert overview["2822"]["personal_best_s"] == 40.762
    assert overview["77"]["sport"] == "Cycling"
    assert overview["77"]["activities"] == 1


def test_sport_name_and_icon() -> None:
    assert sport_name("IceSkating") == "Ice skating"
    assert sport_name("Cycling") == "Cycling"
    assert sport_name(None) is None
    assert sport_icon("IceSkating") == "mdi:skate"
    assert sport_icon("Curling") == "mdi:trophy-variant-outline"
