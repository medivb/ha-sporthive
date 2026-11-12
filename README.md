# MYLAPS Sporthive Custom Integration for Home Assistant

Imports MYLAPS Sporthive **practice (training)** results into Home Assistant:

- **Last training** sensors: date, location, sport, laps, distance, best/average/median lap, active and total time, fastest and average speed. They cover the whole training (all its sessions). Best lap and fastest speed include `achieved_at`; the laps sensor carries every lap in its `lap_times` attribute.
- **Overall records per sport**: *Overall best lap <sport>* and *Overall fastest speed <sport>* (e.g. `sensor.sporthive_<chip>_overall_best_lap_ice_skating`), added automatically for every sport, with `achieved_at`, location and track length.
- **Profile picture**: an image entity with the picture of your MYLAPS account.
- **Per location** sensors (added automatically for each rink or track): personal best lap, fastest speed, total distance and number of activities.
- The full history as **long-term statistics**, for the *Statistics graph* card:
  - per location (lap times are only comparable on the same track): `sporthive:<chip>_loc<location id>_best_lap`, `_average_lap`, `_median_lap`, `_fastest_speed`, `_laps`, `_distance`, `_active_time`;
  - overall totals across locations: `sporthive:<chip>_laps`, `_distance`, `_active_time`.

> Unofficial. The author has no professional relation with MYLAPS or Sporthive. It uses the private API behind sporthive.com (`practice-api.speedhive.com`), which can change without notice. Check the MYLAPS terms of use before relying on it.

## Installation

1. HACS (or settings → marketplace) → three dots → *Custom repositories* → add `https://github.com/medivb/ha-sporthive` as type *Integration*, then install **MYLAPS Sporthive Custom Integration**. Or copy `custom_components/sporthive` into your Home Assistant `config/custom_components/` folder.
2. Restart Home Assistant.
3. *Settings → Devices & services → Add integration → MYLAPS Sporthive Custom Integration*.
4. Enter your MYLAPS **email address** and **password**, and the link of any of your activities, e.g. `https://sporthive.com/practice/1234567890/activity`.

## Languages and icon

The setup and options screens are available in English and Dutch. The integration ships its own icon, with a dark-mode variant, in `custom_components/sporthive/brand/` (shown from Home Assistant 2026.3).

## Sign-in

MYLAPS signs in through Azure AD B2C. Its tokens for sporthive.com last 1 hour (access) and 24 hours from sign-in (refresh), and renewing does not extend the 24 hours. The integration therefore:

- renews the 1-hour token with the refresh token (the refresh token is kept across restarts);
- signs in again with your email address and password about once a day, by repeating the steps of the MYLAPS sign-in page.

Consequences:

- Your password is stored in Home Assistant's `.storage/core.config_entries`, unencrypted. Use a password unique to MYLAPS and protect your Home Assistant backups.
- If MYLAPS changes its sign-in page, adds a captcha or MFA, or asks you to accept new terms, the automatic sign-in fails. The log then says so; sign in once on sporthive.com and reload the integration. A wrong password opens a *Re-authenticate* prompt.

## How the history is found

The integration reads your account's activity list (all activities on the first run, the newest 10 afterwards) and fetches each new activity with all its sessions and laps: up to 10 activities every 2 minutes until the history is complete, then a check every 3 hours. The *Activities imported* sensor shows progress (`pending_backfill`) and the number of activities on your account (`activities_on_account`).

Each activity becomes one hourly statistic row in the hour it started. Distance is laps × track length.

## Ignoring activities

*Settings → Devices & services → MYLAPS Sporthive Custom Integration → Configure* lists all activities. Tick the ones to ignore (for example a test session); they are left out of the sensors and statistics, and the statistics are rebuilt.

## Privacy

Only the chip code, your MYLAPS account ID (needed to list your activities) and the training figures are stored. Chip labels and other personal fields from the API are not stored or shown. The local history is kept in `.storage/sporthive.<entry_id>` and deleted when the integration is removed.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest -q
```

Every push runs the tests, Home Assistant's `hassfest` check and the HACS validation (see `.github/workflows/validate.yml`).
