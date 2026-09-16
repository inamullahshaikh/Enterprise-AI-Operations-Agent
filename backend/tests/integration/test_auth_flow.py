"""docs/system-design.md section 26: "SSE resume with Last-Event-ID" etc. are
listed as integration-test examples; this covers the auth-specific ones —
refresh rotation and reuse detection (section 18.2) — against a real Postgres.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

_EMAIL = "alice@example.com"
_PASSWORD = "correct horse battery staple"


async def _register(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": _EMAIL, "password": _PASSWORD, "full_name": "Alice"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_register_then_me(client: AsyncClient) -> None:
    body = await _register(client)
    me_resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == _EMAIL


async def test_duplicate_registration_is_rejected(client: AsyncClient) -> None:
    await _register(client)
    dup = await client.post(
        "/api/v1/auth/register",
        json={"email": _EMAIL, "password": "another password", "full_name": "Alice 2"},
    )
    assert dup.status_code == 409


async def test_login_with_wrong_password_is_rejected(client: AsyncClient) -> None:
    await _register(client)
    bad = await client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": "wrong"})
    assert bad.status_code == 401


async def test_login_with_correct_password_succeeds(client: AsyncClient) -> None:
    await _register(client)
    ok = await client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD})
    assert ok.status_code == 200
    assert "access_token" in ok.json()


async def test_missing_bearer_token_is_rejected(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


async def test_refresh_rotates_the_token_and_mints_a_new_access_token(client: AsyncClient) -> None:
    body = await _register(client)
    old_refresh_cookie = client.cookies.get("relay_refresh")
    assert old_refresh_cookie

    refresh_resp = await client.post("/api/v1/auth/refresh")
    assert refresh_resp.status_code == 200
    assert refresh_resp.json()["access_token"] != body["access_token"]
    assert client.cookies.get("relay_refresh") != old_refresh_cookie


async def test_reusing_a_rotated_refresh_token_revokes_the_whole_family(
    client: AsyncClient,
) -> None:
    await _register(client)
    old_refresh_cookie = client.cookies.get("relay_refresh")

    first_refresh = await client.post("/api/v1/auth/refresh")
    assert first_refresh.status_code == 200
    new_refresh_cookie = client.cookies.get("relay_refresh")

    # Present the token that was already rotated away — reuse detection.
    client.cookies.set("relay_refresh", old_refresh_cookie)
    reuse_resp = await client.post("/api/v1/auth/refresh")
    assert reuse_resp.status_code == 401

    # The legitimately-rotated *current* token is now also revoked, because
    # reuse detection kills the entire family, not just the reused token.
    client.cookies.set("relay_refresh", new_refresh_cookie)
    after_reuse = await client.post("/api/v1/auth/refresh")
    assert after_reuse.status_code == 401


async def test_logout_revokes_the_refresh_token(client: AsyncClient) -> None:
    await _register(client)
    logout_resp = await client.post("/api/v1/auth/logout")
    assert logout_resp.status_code == 204

    refresh_resp = await client.post("/api/v1/auth/refresh")
    assert refresh_resp.status_code == 401
