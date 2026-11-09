"""Config flow, sign-in, setup, back-fill and statistics tests."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant import config_entries
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.sporthive.auth import AUTHORIZE_URL, TOKEN_URL
from custom_components.sporthive.const import (
    USERS_API_BASE,
    API_BASE,
    B2C_BASE,
    CONF_ACCOUNT_ID,
    CONF_CHIP,
    CONF_EXCLUDED,
    CONF_REFRESH_EXPIRES,
    CONF_REFRESH_TOKEN,
    CONF_SEED_ACTIVITY,
    DOMAIN,
)

FIXTURES = Path(__file__).parent / "fixtures"
ACTIVITY = json.loads((FIXTURES / "activity.json").read_text())
SESSIONS = json.loads((FIXTURES / "sessions.json").read_text())
TENANT_PATH = "/mylapsb2cprd.onmicrosoft.com/B2C_1A_signup_signin"
SETTINGS = {
    "api": "CombinedSigninAndSignup",
    "csrf": "csrf-1",
    "transId": "StateProperties=abc",
    "hosts": {"tenant": TENANT_PATH, "policy": "B2C_1A_signup_signin"},
}
SIGNIN_PAGE = f"<html><script>var SETTINGS = {json.dumps(SETTINGS)};</script></html>"
TOKENS = {
    "id_token": "id-token-1",
    "token_type": "Bearer",
    "id_token_expires_in": 3600,
    "scope": "openid",
    "refresh_token": "refresh-1",
    "refresh_token_expires_in": 86400,
}
OLDER = {
    8557340454: "2026-11-01T18:00:00+01:00",
    8524880271: "2026-10-25T18:00:00+01:00",
}
INLINE_ID = 8524880271
USER_INPUT = {
    CONF_USERNAME: "skater@example.com",
    CONF_PASSWORD: "secret",
    "activity": "https://sporthive.com/practice/8582809434/activity",
}


def _mock_login(aioclient_mock, *, signin_status: str = "200", page: str = SIGNIN_PAGE) -> None:
    aioclient_mock.get(
        AUTHORIZE_URL,
        text=page,
        headers=[
            ("Set-Cookie", "x-ms-cpim-csrf=csrf-1; domain=mylapsb2cprd.b2clogin.com; path=/; secure"),
            ("Set-Cookie", "x-ms-cpim-cache:abc_0=cache-1; path=/; secure; HttpOnly"),
            ("Set-Cookie", "x-ms-cpim-trans=trans-1; path=/; secure; HttpOnly"),
        ],
    )
    body = {"status": signin_status}
    if signin_status != "200":
        body["message"] = "Your password is incorrect"
    aioclient_mock.post(f"{B2C_BASE}{TENANT_PATH}/SelfAsserted", text=json.dumps(body))
    aioclient_mock.get(
        f"{B2C_BASE}{TENANT_PATH}/api/CombinedSigninAndSignup/confirmed",
        status=302,
        headers={"Location": "https://sporthive.com/auth?state=x&code=auth-code-1"},
    )
    aioclient_mock.post(TOKEN_URL, json=TOKENS)


def _mock_api(aioclient_mock, *, account_status: int = 200) -> None:
    listing = {
        "activities": [
            {"id": 8582809434, "startTime": ACTIVITY["startTime"]},
            *({"id": i, "startTime": start} for i, start in OLDER.items()),
        ],
        "activityCount": 3,
    }
    aioclient_mock.get(
        f"{API_BASE}/accounts/MYLAPS-GA-test/training/activities",
        json=listing,
        status=account_status,
    )
    aioclient_mock.get(f"{API_BASE}/training/activities/8582809434", json=ACTIVITY)
    aioclient_mock.get(f"{API_BASE}/training/activities/8582809434/sessions", json=SESSIONS)
    for activity_id, start in OLDER.items():
        activity = copy.deepcopy(ACTIVITY)
        activity.update(id=activity_id, startTime=start)
        activity["practiceSessionsDto"]["locations"] = []
        sessions = copy.deepcopy(SESSIONS)
        sessions["stats"]["lapCount"] = 20
        if activity_id == INLINE_ID:
            # Inline track timing half laps: much shorter "laps".
            activity["location"].update(
                id=99, name="Inline track", sport="InlineSkating", trackLength=200
            )
            sessions["stats"]["fastestTime"] = "20.500"
            sessions["stats"]["fastestSpeed"] = {"kph": 38.1}
            sessions["sessions"][0]["laps"][0]["duration"] = "20.500"
            sessions["sessions"][0]["laps"][0]["speed"] = {"kph": 38.1}
        aioclient_mock.get(f"{API_BASE}/training/activities/{activity_id}", json=activity)
        aioclient_mock.get(f"{API_BASE}/training/activities/{activity_id}/sessions", json=sessions)


def _calls(aioclient_mock, method: str, fragment: str) -> list:
    return [c for c in aioclient_mock.mock_calls if c[0].lower() == method and fragment in str(c[1])]


async def _start_flow(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_config_flow_signs_in(hass: HomeAssistant, aioclient_mock) -> None:
    _mock_login(aioclient_mock)
    _mock_api(aioclient_mock)
    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "activity": "nonsense"}
    )
    assert result["errors"] == {"activity": "invalid_activity"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Sporthive KX-00000"
    data = result["data"]
    assert data[CONF_USERNAME] == "skater@example.com"
    assert data[CONF_CHIP] == "KX-00000"
    assert data[CONF_SEED_ACTIVITY] == 8582809434
    assert data[CONF_ACCOUNT_ID] == "MYLAPS-GA-test"
    assert data[CONF_REFRESH_TOKEN] == "refresh-1"
    assert data[CONF_REFRESH_EXPIRES]

    # Credentials posted the way the B2C page does it.
    signin = _calls(aioclient_mock, "post", "SelfAsserted")[0]
    assert signin[2] == {
        "request_type": "RESPONSE",
        "signInName": "skater@example.com",
        "password": "secret",
    }
    assert signin[3]["X-CSRF-TOKEN"] == "csrf-1"
    assert "tx=StateProperties" in str(signin[1])
    # Cookies with a colon in the name (dropped by aiohttp's jar) are sent too.
    assert signin[3]["Cookie"] == (
        "x-ms-cpim-csrf=csrf-1; x-ms-cpim-cache:abc_0=cache-1; x-ms-cpim-trans=trans-1"
    )
    confirm = _calls(aioclient_mock, "get", "confirmed")[0]
    assert "x-ms-cpim-cache:abc_0=cache-1" in confirm[3]["Cookie"]

    # Code exchanged with PKCE and the SPA origin.
    token_call = _calls(aioclient_mock, "post", "oauth2/v2.0/token")[0]
    assert token_call[2]["grant_type"] == "authorization_code"
    assert token_call[2]["code"] == "auth-code-1"
    assert len(token_call[2]["code_verifier"]) >= 43
    assert token_call[3]["Origin"] == "https://sporthive.com"

    api_call = _calls(aioclient_mock, "get", "practice-api")[-1]
    assert api_call[3]["Authorization"] == "Bearer id-token-1"
    # The practice API answers 401 without the sporthive.com Origin header.
    assert api_call[3]["Origin"] == "https://sporthive.com"


async def test_config_flow_wrong_password(hass: HomeAssistant, aioclient_mock) -> None:
    _mock_login(aioclient_mock, signin_status="400")
    _mock_api(aioclient_mock)
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "invalid_auth"}
    assert not _calls(aioclient_mock, "post", "oauth2/v2.0/token")


async def test_config_flow_changed_login_page(hass: HomeAssistant, aioclient_mock) -> None:
    _mock_login(aioclient_mock, page="<html>new design</html>")
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "login_flow"}


def _entry(**extra) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="KX-00000",
        data={
            CONF_USERNAME: "skater@example.com",
            CONF_PASSWORD: "secret",
            CONF_CHIP: "KX-00000",
            CONF_SEED_ACTIVITY: 8582809434,
            **extra,
        },
    )


async def test_setup_uses_stored_refresh_token(
    hass: HomeAssistant, aioclient_mock
) -> None:
    _mock_login(aioclient_mock)
    _mock_api(aioclient_mock)
    expires = datetime.now(UTC) + timedelta(hours=10)
    entry = _entry(**{CONF_REFRESH_TOKEN: "stored", CONF_REFRESH_EXPIRES: expires.isoformat()})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert not _calls(aioclient_mock, "get", "authorize")  # no full sign-in
    token_calls = _calls(aioclient_mock, "post", "oauth2/v2.0/token")
    assert len(token_calls) == 1
    assert token_calls[0][2]["grant_type"] == "refresh_token"
    assert token_calls[0][2]["refresh_token"] == "stored"
    # SPA refresh tokens keep their original expiry.
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-1"
    assert datetime.fromisoformat(entry.data[CONF_REFRESH_EXPIRES]) == expires


async def test_setup_backfill_and_statistics(
    recorder_mock, hass: HomeAssistant, aioclient_mock
) -> None:
    _mock_login(aioclient_mock)
    _mock_api(aioclient_mock)
    entry = _entry()
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "kx-00000_overall_best_lap", config_entry=entry
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    best = hass.states.get("sensor.sporthive_kx_00000_last_training_best_lap")
    assert best.attributes["achieved_at"] == "2026-11-07T17:05:25.399+01:00"
    # Records per sport: inline half laps do not beat the ice record.
    record = hass.states.get("sensor.sporthive_kx_00000_overall_best_lap_ice_skating")
    assert record.state == "40.762"
    assert record.attributes["location"] == "IJsbaan Haarlem Scoreboard"
    assert record.attributes["sport"] == "Ice skating"
    assert record.attributes["track_length_m"] == 400
    assert record.attributes["achieved_at"].startswith("2026-")
    assert record.attributes["icon"] == "mdi:skate"
    inline = hass.states.get("sensor.sporthive_kx_00000_overall_best_lap_inline_skating")
    assert inline.state == "20.5"
    assert inline.attributes["location"] == "Inline track"
    assert inline.attributes["track_length_m"] == 200
    speed = hass.states.get("sensor.sporthive_kx_00000_overall_fastest_speed_ice_skating")
    assert speed.state == "35.327"
    assert "achieved_at" in speed.attributes
    assert hass.states.get("sensor.sporthive_kx_00000_overall_fastest_speed_inline_skating").state == "38.1"
    # The combined v0.4 record sensors are removed.
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, "kx-00000_overall_best_lap") is None
    assert len(_calls(aioclient_mock, "get", "authorize")) == 1  # one sign-in only

    assert hass.states.get("sensor.sporthive_kx_00000_last_training_best_lap").state == "40.762"
    assert hass.states.get("sensor.sporthive_kx_00000_last_training_distance").state == "10.4"
    assert hass.states.get("sensor.sporthive_kx_00000_last_training_location").state == "IJsbaan Haarlem Scoreboard"
    assert hass.states.get("sensor.sporthive_kx_00000_activities_imported").state == "3"
    laps = hass.states.get("sensor.sporthive_kx_00000_last_training_laps")
    assert laps.state == "26"
    assert len(laps.attributes["lap_times"]) == 4
    assert entry.runtime_data.update_interval == timedelta(hours=3)

    await async_wait_recording_done(hass)
    ids = await recorder_mock.async_add_executor_job(list_statistic_ids, hass)
    stat_ids = {s["statistic_id"] for s in ids}
    # Lap times per location; laps, distance and time also as overall totals.
    assert {
        "sporthive:kx_00000_loc2822_best_lap",
        "sporthive:kx_00000_loc2822_distance",
        "sporthive:kx_00000_distance",
        "sporthive:kx_00000_laps",
    } <= stat_ids
    assert "sporthive:kx_00000_best_lap" not in stat_ids

    sport = hass.states.get("sensor.sporthive_kx_00000_last_training_sport")
    assert sport.state == "Ice skating"
    assert sport.attributes["icon"] == "mdi:skate"
    assert sport.attributes["sport_code"] == "IceSkating"
    best = hass.states.get("sensor.sporthive_kx_00000_last_training_best_lap")
    assert best.attributes["location"] == "IJsbaan Haarlem Scoreboard"
    assert best.attributes["sport"] == "Ice skating"
    location = hass.states.get("sensor.sporthive_kx_00000_last_training_location")
    assert location.attributes["country"] == "nl"
    assert location.attributes["track_length_m"] == 400

    pb = hass.states.get("sensor.sporthive_kx_00000_ijsbaan_haarlem_scoreboard_personal_best_lap")
    assert pb.state == "40.762"
    assert pb.attributes["sport"] == "Ice skating"
    assert hass.states.get("sensor.sporthive_kx_00000_ijsbaan_haarlem_scoreboard_activities").state == "2"
    assert hass.states.get("sensor.sporthive_kx_00000_inline_track_activities").state == "1"
    # Account list used: the account ID was derived from the seed activity.
    assert entry.data[CONF_ACCOUNT_ID] == "MYLAPS-GA-test"
    assert _calls(aioclient_mock, "get", "/accounts/MYLAPS-GA-test/training/activities")

    stats = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utcnow() - timedelta(days=3650),
        None,
        {"sporthive:kx_00000_laps"},
        "hour",
        None,
        {"state", "sum"},
    )
    rows = stats["sporthive:kx_00000_laps"]
    assert [r["state"] for r in rows] == [20, 20, 26]
    assert [r["sum"] for r in rows] == [20, 40, 66]


async def test_wrong_password_starts_reauth(hass: HomeAssistant, aioclient_mock) -> None:
    _mock_login(aioclient_mock, signin_status="400")
    entry = _entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_v01_token_entry_asks_for_credentials(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="KX-00000",
        data={"token": "old", CONF_CHIP: "KX-00000", CONF_SEED_ACTIVITY: 8582809434},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_config_flow_api_rejects_token(hass: HomeAssistant, aioclient_mock) -> None:
    """Sign-in works but the practice API answers 401 (regression: no crash)."""
    _mock_login(aioclient_mock)
    aioclient_mock.get(f"{API_BASE}/training/activities/8582809434", status=401, text="")
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "token_rejected"}
    # Retried once with a refreshed token before giving up.
    assert len(_calls(aioclient_mock, "get", "practice-api")) == 2
    grants = [c[2]["grant_type"] for c in _calls(aioclient_mock, "post", "oauth2/v2.0/token")]
    assert grants == ["authorization_code", "refresh_token"]


async def _laps_rows(hass, recorder_mock) -> list:
    await async_wait_recording_done(hass)
    stats = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utcnow() - timedelta(days=3650),
        None,
        {"sporthive:kx_00000_laps"},
        "hour",
        None,
        {"state", "sum"},
    )
    return stats.get("sporthive:kx_00000_laps", [])


async def test_exclude_oldest_activity(recorder_mock, hass: HomeAssistant, aioclient_mock) -> None:
    _mock_login(aioclient_mock)
    _mock_api(aioclient_mock)
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert [r["sum"] for r in await _laps_rows(hass, recorder_mock)] == [20, 40, 66]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EXCLUDED: ["8524880271"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_EXCLUDED] == ["8524880271"]
    activities = hass.states.get("sensor.sporthive_kx_00000_activities_imported")
    assert activities.state == "2"
    assert activities.attributes["excluded"] == 1
    # The statistics were rebuilt without the oldest activity.
    assert [r["sum"] for r in await _laps_rows(hass, recorder_mock)] == [20, 46]
    # The excluded activity is not fetched again.
    assert len(_calls(aioclient_mock, "get", "/training/activities/8524880271/sessions")) == 1


async def test_refresh_token_update_does_not_reload(
    recorder_mock, hass: HomeAssistant, aioclient_mock
) -> None:
    _mock_login(aioclient_mock)
    _mock_api(aioclient_mock)
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_REFRESH_TOKEN: "x"})
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator


async def test_fallback_to_activity_links(recorder_mock, hass: HomeAssistant, aioclient_mock) -> None:
    _mock_login(aioclient_mock)
    _mock_api(aioclient_mock, account_status=404)
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.sporthive_kx_00000_activities_imported").state == "3"


async def test_profile_picture(
    recorder_mock, hass: HomeAssistant, aioclient_mock, hass_client
) -> None:
    _mock_login(aioclient_mock)
    _mock_api(aioclient_mock)
    aioclient_mock.get(
        f"{USERS_API_BASE}/api/v2/image/id/MYLAPS-GA-test",
        content=b"\x89PNG fake",
        headers={"Content-Type": "image/png"},
    )
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("image.sporthive_kx_00000_profile_picture")
    client = await hass_client()
    resp = await client.get("/api/image_proxy/image.sporthive_kx_00000_profile_picture")
    assert resp.status == 200
    assert await resp.read() == b"\x89PNG fake"
    assert resp.headers["Content-Type"] == "image/png"
    call = _calls(aioclient_mock, "get", "usersandproducts-api")[0]
    assert call[3]["Authorization"] == "Bearer id-token-1"
    assert call[3]["Origin"] == "https://sporthive.com"
