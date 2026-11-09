"""End-to-end tests with mocked Salto KS / Clay endpoints."""

from __future__ import annotations

from datetime import timedelta
import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.saltoks.const import API_BASE_URL, DOMAIN, TOKEN_URL

ENTRY_DATA = {
    "username": "user@example.com",
    "password": "pw",
    "client_id": "my-client",
    "client_secret": "",
    "scope": "",
}

ME = {"id": "u1", "first_name": "Test", "last_name": "User", "email": "t@example.com",
      "image_url": "https://img.example.com/u1.png", "language": "en"}
SITES = {"items": [{"id": "s1", "customer_reference": "HQ Amsterdam", "name": "hq",
                    "timezone": "Europe/Amsterdam", "is_active": True,
                    "owner": {"first_name": "Owner", "image_url": "https://img.example.com/o.png"}}]}
LOCKS = [{"id": "l1", "customer_reference": "Front door", "locked_state": "locked",
          "battery_level": "normal", "online": True,
          "last_seen_at": "2026-11-09T10:00:00Z", "tags": ["a", "b"]}]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _token(aioclient_mock, status=200, access="tok1"):
    body = ({"access_token": access, "refresh_token": "r1", "expires_in": 3600}
            if status == 200 else {"error": "invalid_grant"})
    aioclient_mock.post(TOKEN_URL, status=status, json=body)


def _api(aioclient_mock, locks=None, me=None):
    aioclient_mock.get(f"{API_BASE_URL}/me", json=me or ME)
    aioclient_mock.get(f"{API_BASE_URL}/sites", json=SITES)
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks", json=locks or LOCKS)


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_devices_and_entities(hass: HomeAssistant, aioclient_mock) -> None:
    _token(aioclient_mock)
    _api(aioclient_mock)
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    # Token request: password grant with Basic client auth.
    method, url, data, headers = aioclient_mock.mock_calls[0]
    assert str(url) == TOKEN_URL
    assert data["grant_type"] == "password"
    assert headers["Authorization"].startswith("Basic ")

    devreg = dr.async_get(hass)
    account = devreg.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    site = devreg.async_get_device(identifiers={(DOMAIN, "site_s1")})
    assert account.name == "Test User"
    assert site.name == "HQ Amsterdam"
    assert site.via_device_id == account.id

    entreg = er.async_get(hass)
    lock_id = entreg.async_get_entity_id("lock", DOMAIN, f"{entry.entry_id}_lock_s1_l1")
    assert hass.states.get(lock_id).state == "locked"
    assert entreg.async_get(lock_id).device_id == site.id

    image_id = entreg.async_get_entity_id("image", DOMAIN, f"{entry.entry_id}_profile_image")
    assert entreg.async_get(image_id).device_id == account.id
    assert hass.states.get(image_id).state != "unavailable"

    # Metadata entities.
    def uid(x):
        return f"{entry.entry_id}_{x}"
    lang = entreg.async_get_entity_id("sensor", DOMAIN, uid("me__language"))
    assert hass.states.get(lang).state == "en"
    tz = entreg.async_get_entity_id("sensor", DOMAIN, uid("site_s1__timezone"))
    assert hass.states.get(tz).state == "Europe/Amsterdam"
    active = entreg.async_get_entity_id("binary_sensor", DOMAIN, uid("site_s1__is_active"))
    assert hass.states.get(active).state == "on"
    seen = entreg.async_get_entity_id("sensor", DOMAIN, uid("lock_s1_l1__last_seen_at"))
    assert hass.states.get(seen).attributes["device_class"] == "timestamp"
    tags = entreg.async_get_entity_id("sensor", DOMAIN, uid("lock_s1_l1__tags"))
    assert hass.states.get(tags).state == "2"
    assert hass.states.get(tags).attributes["items"] == ["a", "b"]
    online = entreg.async_get_entity_id("binary_sensor", DOMAIN, uid("lock_s1_l1__online"))
    assert entreg.async_get(online).device_id == site.id


async def test_lock_unlock_and_new_metadata(hass: HomeAssistant, aioclient_mock) -> None:
    _token(aioclient_mock)
    _api(aioclient_mock)
    entry = await _setup(hass)
    lock_id = er.async_get(hass).async_get_entity_id(
        "lock", DOMAIN, f"{entry.entry_id}_lock_s1_l1")

    patch_url = f"{API_BASE_URL}/sites/s1/locks/l1/locking"
    aioclient_mock.patch(patch_url, json={})
    # After unlocking, the API reports office_mode and a brand-new field.
    unlocked = [{**LOCKS[0], "locked_state": "office_mode", "firmware": "1.2.3"}]
    aioclient_mock.clear_requests()
    _token(aioclient_mock)
    _api(aioclient_mock, locks=unlocked)
    aioclient_mock.patch(patch_url, json={})

    await hass.services.async_call("lock", "unlock", {"entity_id": lock_id}, blocking=True)
    patch_calls = [c for c in aioclient_mock.mock_calls if c[0] == "PATCH"]
    assert patch_calls[-1][2] == {"locked_state": "office_mode"}

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert hass.states.get(lock_id).state == "unlocked"

    fw = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_lock_s1_l1__firmware")
    assert fw is not None and hass.states.get(fw).state == "1.2.3"

    await hass.services.async_call("lock", "lock", {"entity_id": lock_id}, blocking=True)
    patch_calls = [c for c in aioclient_mock.mock_calls if c[0] == "PATCH"]
    assert patch_calls[-1][2] == {"locked_state": "locked"}


async def test_invalid_credentials_start_reauth(hass: HomeAssistant, aioclient_mock) -> None:
    _token(aioclient_mock, status=400)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_client_token_lifecycle(hass: HomeAssistant, aioclient_mock) -> None:
    """Refresh grant on expiry, password grant when refresh is rejected, retry on 401."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.saltoks.api import SaltoKSClient

    client = SaltoKSClient(async_get_clientsession(hass), "u", "p", "c")
    _token(aioclient_mock)
    await client.async_get_access_token()
    assert aioclient_mock.mock_calls[-1][2]["grant_type"] == "password"

    client._expires_at = 0  # simulate expiry
    await client.async_get_access_token()
    assert aioclient_mock.mock_calls[-1][2]["grant_type"] == "refresh_token"

    # Refresh rejected -> password grant.
    aioclient_mock.clear_requests()
    calls = []

    async def token_handler(method, url, data):
        calls.append(data["grant_type"])
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
        if data["grant_type"] == "refresh_token":
            return AiohttpClientMockResponse(method, url, status=400,
                                             text=json.dumps({"error": "invalid_grant"}))
        return AiohttpClientMockResponse(method, url, status=200, text=json.dumps(
            {"access_token": "tok2", "refresh_token": "r2", "expires_in": 3600}))

    aioclient_mock.post(TOKEN_URL, side_effect=token_handler)
    client._expires_at = 0
    assert await client.async_get_access_token() == "tok2"
    assert calls == ["refresh_token", "password"]


def test_basic_auth_formats() -> None:
    import base64
    from custom_components.saltoks.api import SaltoKSClient

    # No secret: client ID sent as-is, matching the manual curl request.
    assert SaltoKSClient(None, "u", "p", "abc123==")._basic_auth == "Basic abc123=="
    assert SaltoKSClient(None, "u", "p", " Basic abc123== ")._basic_auth == "Basic abc123=="
    with_secret = base64.b64encode(b"id:sec").decode()
    assert SaltoKSClient(None, "u", "p", "id", "sec")._basic_auth == f"Basic {with_secret}"


async def test_token_request_matches_manual_call(hass: HomeAssistant, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.saltoks.api import SaltoKSClient

    _token(aioclient_mock)
    client = SaltoKSClient(async_get_clientsession(hass), "a@b.c", "pw", "CLIENTID")
    await client.async_authenticate()
    _, url, data, headers = aioclient_mock.mock_calls[-1]
    assert headers["Authorization"] == "Basic CLIENTID"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert data == {"grant_type": "password", "username": "a@b.c", "password": "pw"}


async def test_config_flow_shows_server_error(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(TOKEN_URL, status=400,
                        json={"error": "invalid_client", "error_description": "unknown client"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "invalid_client"}
    assert result["description_placeholders"]["server_message"] == "invalid_client: unknown client"


async def test_picture_urls_are_images_not_sensors(hass: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1")
    entry.add_to_hass(hass)
    entreg = er.async_get(hass)
    # Sensors created for picture URLs by an earlier version must be removed.
    for uid in ("me__image_url", "site_s1__owner.image_url"):
        entreg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_{uid}",
                                   config_entry=entry)
    _token(aioclient_mock)
    _api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for uid in ("me__image_url", "site_s1__owner.image_url"):
        assert entreg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{uid}") is None
    # Other owner fields stay sensors.
    assert entreg.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_site_s1__owner.first_name") is not None

    site = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "site_s1")})
    owner_img = entreg.async_get_entity_id(
        "image", DOMAIN, f"{entry.entry_id}_site_s1__owner.image_url")
    assert entreg.async_get(owner_img).device_id == site.id
    state = hass.states.get(owner_img)
    assert state.state != "unavailable"
    assert state.attributes["friendly_name"] == "HQ Amsterdam Owner picture"

    # The profile image on the account device is unchanged.
    assert entreg.async_get_entity_id(
        "image", DOMAIN, f"{entry.entry_id}_profile_image") is not None
    assert entreg.async_get_entity_id(
        "image", DOMAIN, f"{entry.entry_id}_me__image_url") is None

    # A changed URL updates the picture timestamp.
    before = state.state
    new_sites = {"items": [{**SITES["items"][0],
                            "owner": {"first_name": "Owner", "image_url": "https://img.example.com/o2.png"}}]}
    aioclient_mock.clear_requests()
    _token(aioclient_mock)
    aioclient_mock.get(f"{API_BASE_URL}/me", json=ME)
    aioclient_mock.get(f"{API_BASE_URL}/sites", json=new_sites)
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks", json=LOCKS)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(owner_img).state != before


async def test_id_entities_hidden_by_default(hass: HomeAssistant, aioclient_mock) -> None:
    _token(aioclient_mock)
    _api(aioclient_mock)
    entry = await _setup(hass)
    entreg = er.async_get(hass)

    def reg(uid):
        return entreg.async_get(entreg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{uid}"))

    for uid in ("me__id", "site_s1__id", "lock_s1_l1__id"):
        assert reg(uid).hidden_by is er.RegistryEntryHider.INTEGRATION
    for uid in ("me__language", "site_s1__timezone", "site_s1__owner.first_name"):
        assert reg(uid).hidden_by is None


async def test_migration_hides_existing_id_entities(hass: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1",
                            version=1, minor_version=1)
    entry.add_to_hass(hass)
    entreg = er.async_get(hass)
    old_id = entreg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_site_s1__id",
                                        config_entry=entry)
    old_tz = entreg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_site_s1__timezone",
                                        config_entry=entry)
    _token(aioclient_mock)
    _api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 2
    assert entreg.async_get(old_id.entity_id).hidden_by is er.RegistryEntryHider.INTEGRATION
    assert entreg.async_get(old_tz.entity_id).hidden_by is None


def test_is_id_field() -> None:
    from custom_components.saltoks.entity import is_id_field

    for path in [("id",), ("owner", "id"), ("site_id",), ("customerId",), ("uuid",)]:
        assert is_id_field(path), path
    for path in [("idle",), ("valid",), ("android",), ("customer_reference",), ()]:
        assert not is_id_field(path), path


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


async def test_picture_served_as_octet_stream(hass: HomeAssistant, aioclient_mock) -> None:
    """Azure Blob Storage serves pictures as application/octet-stream."""
    import respx

    _token(aioclient_mock)
    _api(aioclient_mock)
    entry = await _setup(hass)
    owner_img = er.async_get(hass).async_get_entity_id(
        "image", DOMAIN, f"{entry.entry_id}_site_s1__owner.image_url")
    entity = hass.data["image"].get_entity(owner_img)

    with respx.mock:
        respx.get("https://img.example.com/o.png").respond(
            200, content=PNG_BYTES, headers={"content-type": "application/octet-stream"})
        assert await entity.async_image() == PNG_BYTES
    assert entity.content_type == "image/png"


def test_detect_image_type() -> None:
    from custom_components.saltoks.image import detect_image_type

    assert detect_image_type(PNG_BYTES, "application/octet-stream", "https://x/a") == "image/png"
    assert detect_image_type(b"\xff\xd8\xff\xe0", None, "https://x/a") == "image/jpeg"
    assert detect_image_type(b"", "image/webp", "https://x/a") == "image/webp"
    assert detect_image_type(b"????", "application/octet-stream", "https://x/a.jpg") == "image/jpeg"
    assert detect_image_type(b"????", "application/octet-stream", "https://x/a") is None
