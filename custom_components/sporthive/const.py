"""Constants for the Sporthive integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "sporthive"

API_BASE = "https://practice-api.speedhive.com/api/v1"
USERS_API_BASE = "https://usersandproducts-api.speedhive.com"
ACTIVITY_URL = "https://sporthive.com/practice/{activity_id}/activity"

# MYLAPS sign-in (Azure AD B2C), as used by sporthive.com.
B2C_BASE = "https://mylapsb2cprd.b2clogin.com"
B2C_TENANT = "mylapsb2cprd.onmicrosoft.com"
B2C_POLICY = "B2C_1A_signup_signin"
CLIENT_ID = "d9109f5a-bce9-4ff7-8c8f-71bf4fb68c40"
REDIRECT_URI = "https://sporthive.com/auth"
SPORTHIVE_ORIGIN = "https://sporthive.com"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_REFRESH_EXPIRES = "refresh_token_expires"
CONF_ACTIVITY = "activity"
CONF_CHIP = "chip_code"
CONF_SEED_ACTIVITY = "seed_activity_id"
CONF_ACCOUNT_ID = "account_id"
# Option: activity IDs (as strings) that are not used for sensors or statistics.
CONF_EXCLUDED = "excluded_activities"

# Normal polling interval once the history is complete.
UPDATE_INTERVAL = timedelta(hours=3)
# Polling interval while the history back-fill is still running.
BACKFILL_INTERVAL = timedelta(minutes=2)
# Maximum number of new activities fetched per update (2 requests each).
MAX_NEW_ACTIVITIES_PER_UPDATE = 10
# Number of activities requested from the account list once the history is known.
RECENT_ACTIVITY_COUNT = 10
# Number requested on the first listing (raised to activityCount if needed).
INITIAL_ACTIVITY_COUNT = 200
# Pause between API requests, to stay polite towards the MYLAPS API.
REQUEST_DELAY_SECONDS = 1.0

STORAGE_VERSION = 1
