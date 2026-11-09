"""Run the sign-in against a local fake B2C server with real aiohttp sessions."""

from __future__ import annotations

import json

from aiohttp import web
import pytest

from homeassistant.core import HomeAssistant

from custom_components.sporthive import auth as auth_mod, build_auth

TENANT = "/mylapsb2cprd.onmicrosoft.com/B2C_1A_signup_signin"


@pytest.fixture
async def fake_b2c(socket_enabled):
    seen: dict = {}

    async def authorize(request):
        settings = {
            "api": "CombinedSigninAndSignup",
            "csrf": "csrf-1",
            "transId": "StateProperties=abc",
            "hosts": {"tenant": TENANT, "policy": "B2C_1A_signup_signin"},
        }
        resp = web.Response(
            text=f"<script>var SETTINGS = {json.dumps(settings)};</script>",
            content_type="text/html",
        )
        resp.headers.add("Set-Cookie", "x-ms-cpim-csrf=csrf-1; path=/; HttpOnly")
        resp.headers.add("Set-Cookie", "x-ms-cpim-cache:abc_0=cache-1; path=/; HttpOnly")
        return resp

    async def self_asserted(request):
        seen["cookie"] = request.headers.get("Cookie")
        seen["form"] = dict(await request.post())
        if "x-ms-cpim-cache:abc_0=cache-1" not in (seen["cookie"] or ""):
            return web.Response(status=400, text="<html>Bad Request</html>")
        return web.Response(text='{"status":"200"}', content_type="application/json")

    async def confirmed(request):
        raise web.HTTPFound("https://sporthive.com/auth?code=code-1&state=x")

    async def token(request):
        seen["token_form"] = dict(await request.post())
        return web.json_response(
            {"id_token": "id-1", "id_token_expires_in": 3600,
             "refresh_token": "r-1", "refresh_token_expires_in": 86400}
        )

    app = web.Application()
    app.router.add_get(f"{TENANT.lower()}/oauth2/v2.0/authorize", authorize)
    app.router.add_post(f"{TENANT}/SelfAsserted", self_asserted)
    app.router.add_get(f"{TENANT}/api/CombinedSigninAndSignup/confirmed", confirmed)
    app.router.add_post(f"{TENANT.lower()}/oauth2/v2.0/token", token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", seen
    await runner.cleanup()


async def test_login_with_real_sessions(hass: HomeAssistant, fake_b2c, monkeypatch) -> None:
    base, seen = fake_b2c
    monkeypatch.setattr(auth_mod, "B2C_BASE", base)
    monkeypatch.setattr(auth_mod, "AUTHORIZE_URL", f"{base}{TENANT.lower()}/oauth2/v2.0/authorize")
    monkeypatch.setattr(auth_mod, "TOKEN_URL", f"{base}{TENANT.lower()}/oauth2/v2.0/token")

    auth = build_auth(hass, "user@example.com", "pw")
    assert await auth.async_get_token() == "id-1"
    assert seen["form"]["signInName"] == "user@example.com"
    assert seen["token_form"]["code"] == "code-1"
