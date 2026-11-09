"""Sign-in to MYLAPS (Azure AD B2C) for the Sporthive practice API.

MYLAPS registers sporthive.com as a single-page application. For such apps
Azure AD B2C issues an ID token that is valid for one hour and a refresh token
that expires 24 hours after the interactive sign-in, however often it is
renewed. The integration therefore:

1. uses the refresh token while it is valid (about every hour), and
2. repeats the browser sign-in (authorization code flow with PKCE, posting the
   username and password to the B2C sign-in page) once the refresh token has
   expired, so roughly once a day.

Step 2 imitates the B2C login page and may break when MYLAPS changes it.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp

from .api import SporthiveAuthError, SporthiveConnectionError, SporthiveError
from .const import (
    B2C_BASE,
    B2C_POLICY,
    B2C_TENANT,
    CLIENT_ID,
    REDIRECT_URI,
    SPORTHIVE_ORIGIN,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_URL = f"{B2C_BASE}/{B2C_TENANT}/{B2C_POLICY.lower()}/oauth2/v2.0/token"
AUTHORIZE_URL = f"{B2C_BASE}/{B2C_TENANT}/{B2C_POLICY.lower()}/oauth2/v2.0/authorize"
SCOPE = "openid offline_access"
_SETTINGS_RE = re.compile(r"var\s+SETTINGS\s*=\s*(\{.*?\});", re.DOTALL)
_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Renew tokens this long before they expire.
_MARGIN = timedelta(minutes=5)


class SporthiveLoginFlowError(SporthiveError):
    """The MYLAPS sign-in pages did not behave as expected (they may have changed)."""


@dataclass
class TokenSet:
    """Tokens currently in use."""

    bearer: str
    bearer_expires: datetime
    refresh_token: str | None
    refresh_expires: datetime | None


# Non-personal JWT claims that help diagnose token problems.
_SAFE_CLAIMS = ("iss", "aud", "azp", "scp", "tfp", "acr", "ver", "iat", "exp")


def token_claims(token: str) -> dict[str, Any]:
    """Return the non-personal claims of a JWT (signature not verified)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return {k: claims[k] for k in _SAFE_CLAIMS if k in claims}


def token_claim_names(token: str) -> list[str]:
    """Return the names (not values) of all claims in a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return sorted(json.loads(base64.urlsafe_b64decode(payload)))
    except (IndexError, ValueError):
        return []


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _parse_settings(html: str) -> dict[str, Any]:
    match = _SETTINGS_RE.search(html)
    if not match:
        raise SporthiveLoginFlowError("Sign-in page has no SETTINGS block")
    try:
        settings = json.loads(match.group(1))
    except ValueError as err:
        raise SporthiveLoginFlowError("Could not parse sign-in page SETTINGS") from err
    if not settings.get("csrf") or not settings.get("transId"):
        raise SporthiveLoginFlowError("Sign-in page has no csrf/transId")
    return settings


class _CookieStore:
    """Minimal cookie handling for the B2C sign-in pages.

    B2C sets cookies such as 'x-ms-cpim-cache:<id>_0'. The colon is not
    allowed in cookie names by RFC 6265, so aiohttp's cookie jar silently
    drops them, and B2C then rejects the credential post with HTTP 400.
    These cookies are therefore kept here and sent by hand.
    """

    def __init__(self) -> None:
        self._cookies: dict[str, str] = {}

    def update(self, resp: aiohttp.ClientResponse) -> None:
        for response in (*(getattr(resp, "history", None) or ()), resp):
            for header in response.headers.getall("Set-Cookie", []):
                pair, _, attributes = header.partition(";")
                name, sep, value = pair.partition("=")
                name = name.strip()
                if not sep or not name:
                    continue
                attrs = attributes.lower().replace(" ", "")
                if not value.strip() or "max-age=0" in attrs or "expires=thu,01jan1970" in attrs:
                    self._cookies.pop(name, None)
                else:
                    self._cookies[name] = value.strip()

    def header(self) -> dict[str, str]:
        if not self._cookies:
            return {}
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in self._cookies.items())}

    def names(self) -> list[str]:
        return sorted(self._cookies)

    def get(self, name: str) -> str | None:
        return self._cookies.get(name)


def _snippet(text: str) -> str:
    """Shorten a response body for the log."""
    return re.sub(r"\s+", " ", text)[:300]


def _code_from_location(location: str) -> str:
    parts = urlsplit(location)
    params = parse_qs(parts.query) or parse_qs(parts.fragment)
    if "error" in params:
        description = params.get("error_description", [""])[0]
        raise SporthiveLoginFlowError(f"Sign-in returned an error: {params['error'][0]} {description}")
    if "code" not in params:
        raise SporthiveLoginFlowError("Sign-in did not return an authorization code")
    return params["code"][0]


class SporthiveAuth:
    """Provides a valid bearer token, signing in again when needed."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        login_session_factory: Callable[[], aiohttp.ClientSession],
        username: str,
        password: str,
        refresh_token: str | None = None,
        refresh_expires: datetime | None = None,
        on_refresh_token: Callable[[str, datetime | None], None] | None = None,
    ) -> None:
        self._session = session
        self._login_session_factory = login_session_factory
        self._username = username
        self._password = password
        self._on_refresh_token = on_refresh_token
        self._tokens: TokenSet | None = None
        if refresh_token:
            self._tokens = TokenSet("", datetime.fromtimestamp(0, UTC), refresh_token, refresh_expires)

    def invalidate(self) -> None:
        """Forget the bearer token (after the API rejected it)."""
        if self._tokens:
            self._tokens.bearer = ""
            self._tokens.bearer_expires = datetime.fromtimestamp(0, UTC)

    async def async_get_token(self) -> str:
        """Return a bearer token that is valid for at least a few minutes."""
        now = datetime.now(UTC)
        tokens = self._tokens
        if tokens and tokens.bearer and tokens.bearer_expires - _MARGIN > now:
            return tokens.bearer
        if tokens and tokens.refresh_token and (
            tokens.refresh_expires is None or tokens.refresh_expires - _MARGIN > now
        ):
            try:
                return await self._async_refresh(tokens.refresh_token)
            except SporthiveAuthError:
                _LOGGER.debug("Refresh token rejected; signing in again")
        return await self._async_login()

    # -- token endpoint ------------------------------------------------------

    async def _async_token_request(self, form: dict[str, str]) -> str:
        form = {"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "scope": SCOPE, **form}
        try:
            async with self._session.post(
                TOKEN_URL,
                data=form,
                # B2C only redeems SPA tokens in cross-origin requests.
                headers={"Origin": SPORTHIVE_ORIGIN, "Accept": "application/json"},
                timeout=_TIMEOUT,
            ) as resp:
                status = resp.status
                _LOGGER.debug("Token request (%s): %s", form.get("grant_type"), status)
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise SporthiveConnectionError(f"Token request failed: {err}") from err

        if status != 200:
            description = (payload or {}).get("error_description", "") if isinstance(payload, dict) else ""
            if status in (400, 401):
                raise SporthiveAuthError(f"Token request rejected: {description[:200]}")
            raise SporthiveConnectionError(f"Token request returned {status}")

        bearer = payload.get("access_token") or payload.get("id_token")
        if not bearer:
            raise SporthiveLoginFlowError("Token response has no access_token or id_token")
        now = datetime.now(UTC)
        lifetime = payload.get("expires_in") or payload.get("id_token_expires_in") or 3600
        refresh_token = payload.get("refresh_token")
        refresh_expires: datetime | None = None
        if refresh_token and payload.get("refresh_token_expires_in"):
            refresh_expires = now + timedelta(seconds=int(payload["refresh_token_expires_in"]))
        # A refresh keeps the original expiry for SPA tokens; never extend it.
        if (
            form["grant_type"] == "refresh_token"
            and self._tokens
            and self._tokens.refresh_expires
            and refresh_expires
        ):
            refresh_expires = min(refresh_expires, self._tokens.refresh_expires)

        self._tokens = TokenSet(
            bearer=bearer,
            bearer_expires=now + timedelta(seconds=int(lifetime)),
            refresh_token=refresh_token or (self._tokens.refresh_token if self._tokens else None),
            refresh_expires=refresh_expires or (self._tokens.refresh_expires if self._tokens else None),
        )
        _LOGGER.debug(
            "Token request (%s) returned %s; bearer length %s, claim names %s, claims: %s",
            form["grant_type"],
            "access_token" if payload.get("access_token") else "id_token",
            len(bearer),
            token_claim_names(bearer),
            token_claims(bearer),
        )
        if refresh_token and self._on_refresh_token:
            self._on_refresh_token(refresh_token, self._tokens.refresh_expires)
        return bearer

    async def _async_refresh(self, refresh_token: str) -> str:
        return await self._async_token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    # -- interactive sign-in -------------------------------------------------

    async def _async_login(self) -> str:
        _LOGGER.debug("Signing in to MYLAPS")
        verifier, challenge = _pkce_pair()
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "response_mode": "query",
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": secrets.token_urlsafe(16),
            "nonce": secrets.token_urlsafe(16),
        }
        session = self._login_session_factory()
        try:
            code = await self._async_login_steps(session, params)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SporthiveConnectionError(f"Sign-in failed: {err!r}") from err
        except SporthiveError:
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error during MYLAPS sign-in")
            raise SporthiveLoginFlowError(f"Unexpected error during sign-in: {err!r}") from err
        finally:
            # Release the session without closing the shared connector.
            session.detach()
        return await self._async_token_request(
            {"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
        )

    async def _async_login_steps(self, session: aiohttp.ClientSession, params: dict[str, str]) -> str:
        cookies = _CookieStore()

        # 1. Open the sign-in page.
        async with session.get(AUTHORIZE_URL, params=params, timeout=_TIMEOUT) as resp:
            cookies.update(resp)
            if resp.status != 200:
                raise SporthiveLoginFlowError(f"Sign-in page returned {resp.status}")
            referer = str(resp.url)
            _LOGGER.debug("Sign-in page: %s %s", resp.status, resp.headers.get("Content-Type"))
            settings = _parse_settings(await resp.text())
        _LOGGER.debug(
            "Sign-in page set cookies %s; settings keys %s", cookies.names(), sorted(settings)
        )

        csrf = settings["csrf"]
        trans_id = settings["transId"]
        hosts = settings.get("hosts") or {}
        policy = hosts.get("policy") or B2C_POLICY
        tenant_path = hosts.get("tenant") or f"/{B2C_TENANT}/{policy}"
        api = settings.get("api") or "CombinedSigninAndSignup"
        query = {"tx": trans_id, "p": policy}

        # 2. Post the credentials, as the page's own script does.
        async with session.post(
            f"{B2C_BASE}{tenant_path}/SelfAsserted",
            params=query,
            data={
                "request_type": "RESPONSE",
                "signInName": self._username,
                "password": self._password,
            },
            headers={
                "X-CSRF-TOKEN": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Origin": B2C_BASE,
                "Referer": referer,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                **cookies.header(),
            },
            timeout=_TIMEOUT,
        ) as resp:
            cookies.update(resp)
            body = await resp.text()
            _LOGGER.debug("Credential post: %s %s", resp.status, resp.headers.get("Content-Type"))
            try:
                result = json.loads(body)
            except ValueError as err:
                _LOGGER.warning(
                    "Unexpected sign-in response %s (%s), cookies sent: %s, body: %s",
                    resp.status,
                    resp.headers.get("Content-Type"),
                    cookies.names(),
                    _snippet(body),
                )
                raise SporthiveLoginFlowError(f"Unexpected sign-in response ({resp.status})") from err
        if str(result.get("status")) != "200":
            # B2C answers 400 with a user-facing message for bad credentials.
            raise SporthiveAuthError(f"Sign-in rejected: {str(result.get('message', ''))[:200]}")

        # 3. Confirm; B2C redirects to sporthive.com/auth with the code.
        async with session.get(
            f"{B2C_BASE}{tenant_path}/api/{api}/confirmed",
            params={"rememberMe": "false", "csrf_token": csrf, **query},
            headers={"Referer": referer, **cookies.header()},
            allow_redirects=False,
            timeout=_TIMEOUT,
        ) as resp:
            location = resp.headers.get("Location")
            _LOGGER.debug(
                "Sign-in confirmation: %s, redirect to %s",
                resp.status,
                (location or "").split("?")[0].split("#")[0],
            )
            if resp.status not in (301, 302, 303) or not location:
                # Another page (terms update, MFA, captcha) needs a human.
                _LOGGER.warning(
                    "Sign-in confirmation returned %s: %s", resp.status, _snippet(await resp.text())
                )
                raise SporthiveLoginFlowError(
                    f"Sign-in needs an extra step ({resp.status}); sign in once on sporthive.com"
                )
        return _code_from_location(location)
