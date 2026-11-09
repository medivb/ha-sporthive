"""Minimal client for the MYLAPS practice API used by Sporthive."""

from __future__ import annotations

from typing import Any, Protocol

import logging
from urllib.parse import quote

import aiohttp

from .const import API_BASE, SPORTHIVE_ORIGIN, USERS_API_BASE

_LOGGER = logging.getLogger(__name__)


class SporthiveError(Exception):
    """Base error for the Sporthive API."""


class SporthiveAuthError(SporthiveError):
    """The bearer token is missing, invalid or expired."""


class SporthiveTokenRejectedError(SporthiveAuthError):
    """Sign-in worked, but the practice API did not accept the token."""


class SporthiveNotFoundError(SporthiveError):
    """The requested activity does not exist or is not visible to this account."""


class SporthiveConnectionError(SporthiveError):
    """The API could not be reached or returned an unexpected response."""


class TokenProvider(Protocol):
    """Anything that can hand out a bearer token."""

    async def async_get_token(self) -> str:
        """Return a valid bearer token."""

    def invalidate(self) -> None:
        """Forget the current bearer token."""


class StaticToken:
    """A fixed bearer token (used for tests and manual tokens)."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def async_get_token(self) -> str:
        return self._token

    def invalidate(self) -> None:
        return None


class SporthiveClient:
    """Read-only access to practice activities."""

    def __init__(self, session: aiohttp.ClientSession, auth: TokenProvider) -> None:
        self._session = session
        self._auth = auth

    async def _get(self, path: str, *, base: str = API_BASE, binary: bool = False) -> Any:
        try:
            return await self._get_once(path, base, binary)
        except SporthiveAuthError:
            # The token may have been revoked early; get a new one and retry once.
            self._auth.invalidate()
            return await self._get_once(path, base, binary)

    async def _get_once(self, path: str, base: str, binary: bool) -> Any:
        token = await self._auth.async_get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "image/*" if binary else "application/json",
            # Same as the sporthive.com web app.
            "Origin": SPORTHIVE_ORIGIN,
            "Referer": f"{SPORTHIVE_ORIGIN}/",
        }
        try:
            async with self._session.get(
                f"{base}{path}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (401, 403):
                    body = (await resp.text())[:300]
                    _LOGGER.debug(
                        "API %s rejected the token: %s, WWW-Authenticate: %s, body: %s",
                        path,
                        resp.status,
                        resp.headers.get("WWW-Authenticate"),
                        body,
                    )
                    raise SporthiveTokenRejectedError(f"API rejected the token ({resp.status})")
                if resp.status == 404:
                    raise SporthiveNotFoundError(path)
                if resp.status != 200:
                    raise SporthiveConnectionError(f"Unexpected status {resp.status} for {path}")
                if binary:
                    return await resp.read(), resp.headers.get("Content-Type", "image/jpeg")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise SporthiveConnectionError(str(err)) from err

    async def async_get_activity(self, activity_id: int) -> dict[str, Any]:
        """Return the overview of one practice activity."""
        return await self._get(f"/training/activities/{activity_id}")

    async def async_get_sessions(self, activity_id: int) -> dict[str, Any]:
        """Return the sessions and laps of one practice activity."""
        return await self._get(f"/training/activities/{activity_id}/sessions")

    async def async_get_account_activities(self, account_id: str, count: int) -> dict[str, Any]:
        """Return the newest `count` activities of an account, plus `activityCount`."""
        return await self._get(f"/accounts/{quote(account_id, safe='')}/training/activities?count={count}")

    async def async_get_profile_image(self, account_id: str) -> tuple[bytes, str] | None:
        """Return the account's profile picture and its content type, or None."""
        try:
            return await self._get(
                f"/api/v2/image/id/{quote(account_id, safe='')}",
                base=USERS_API_BASE,
                binary=True,
            )
        except SporthiveNotFoundError:
            return None
