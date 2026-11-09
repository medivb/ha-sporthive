# Salto KS – Home Assistant custom integration

Polls the Salto KS cloud API (default every 10 seconds) and exposes:

| Device | Contents |
|---|---|
| **Account** (main device, named after `/me` first/last name) | Profile image entity (`image_url`), one entity per other `/me` field |
| **Site** (one per site, named after `customer_reference`, linked to the account device) | One `lock` entity per lock, an owner picture image entity (`owner.image_url`), one entity per other site field, one entity per lock field |

## Installation

1. Copy `custom_components/saltoks` into `<config>/custom_components/` (or add this repository to HACS as a custom repository).
2. Restart Home Assistant.
3. *Settings → Devices & services → Add integration → Salto KS*.
4. Enter username, password, client ID, an optional client secret, and the scope.

Polling interval: *Configure* on the integration (5–300 s, default 10 s).

## Authentication

- `POST https://identity.eu.my-clay.com/connect/token` (form-encoded `grant_type=password`, `username`, `password`) with `Authorization: Basic <client_id>`; the client ID is sent exactly as entered. If a client secret is configured, `base64("<client_id>:<client_secret>")` is sent instead. No `scope` is sent unless one is configured.
- The access token is reused until 60 s before expiry, then renewed with `grant_type=refresh_token` when the server issued a refresh token; otherwise a new password grant is performed. Because the integration polls every 10 s, the session is kept alive continuously.
- If refreshing fails, the integration authenticates again with the stored username and password. A `401` from the API forces a token renewal and one retry.
- If the identity server rejects the credentials (`invalid_grant`, `invalid_client`, …), Home Assistant starts a re-authentication flow.
- Tokens are held in memory only; they are not written to disk.

## Locks

| HA action | API call |
|---|---|
| Lock | `PATCH /v1.2/sites/{site_id}/locks/{id}/locking` `{"locked_state": "locked"}` |
| Unlock | same endpoint, `{"locked_state": "office_mode"}` |

State: `locked_state == "locked"` → *locked*; any other value → *unlocked*; missing → *unknown*. The raw value is available as the `locked_state` attribute.

## Metadata entities (auto-discovered)

Every field returned by `/me`, `/sites` and `/sites/{id}/locks` becomes an entity (diagnostic category):

- picture URL fields (`image_url`, `picture_url`, `avatar_url`, `photo_url`, `profile_image_url`, at any nesting level) → `image`, named after the parent object (for example "Owner picture"); no sensor is created for them
- booleans → `binary_sensor`
- strings / numbers → `sensor` (ISO date-time fields named `*_at`, `*date*`, `*time*` … get the timestamp device class)
- lists → `sensor` with the item count as state; full list in the `items` attribute
- nested objects are flattened up to 3 levels (`address.city` → "Address city")
- strings longer than 255 characters are truncated in the state; the full value is in the `value` attribute

Identifier fields (`id`, `*_id`, `*Id`, `uuid`, for example `owner.id`) are **hidden by default**. To show one, open the entity and enable *Visible* under its settings. Entities created by earlier versions are hidden once during the upgrade; a later choice to show them is kept.

New fields, sites or locks that the API returns later are added automatically at the next poll; no update of the integration is needed. Entities for fields that disappear become *unavailable*. Device names follow changes to `customer_reference`.

Diagnostics (*Download diagnostics*) contain all raw API data, with credentials and personal data redacted.

## Assumptions to verify

- **Scope**: none by default. If no refresh token is returned, add `offline_access` (if the client allows it); without it the integration simply re-authenticates with the password grant when the access token expires.
- **Response shapes**: plain JSON arrays and `{"items": [...]}` envelopes are both supported; next-page links (`next_page_link`, `@odata.nextLink`, `next`) are followed when present.
- **Rate limits**: each poll makes 2 + (number of sites) requests (lock lists are fetched in parallel, up to 5 at a time). Increase the interval if Salto KS throttles the account.

## Tests

```bash
pip install pytest-homeassistant-custom-component
pytest
```

