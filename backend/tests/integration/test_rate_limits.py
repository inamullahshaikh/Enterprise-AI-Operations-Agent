"""Phase 8 B3: HTTP rate limits on the message route (docs/system-design.md section 15.6)."""

import uuid

import pytest
from httpx import AsyncClient

from relay_api.deps import get_redis, get_run_dispatcher, get_settings_dep
from relay_api.main import app
from tests.integration.scripted_model import register_workspace_and_conversation

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _limits(client, test_settings):
    """Small limits and a fresh window key per test: the suite shares one Redis."""

    async def _noop(*_: object) -> None:
        return None

    app.dependency_overrides[get_run_dispatcher] = lambda: _noop
    app.dependency_overrides[get_settings_dep] = lambda: test_settings.model_copy(
        update={
            "rate_limit_messages_per_user_min": 3,
            "rate_limit_messages_per_workspace_min": 5,
        }
    )
    yield
    app.dependency_overrides.pop(get_run_dispatcher, None)


async def _send(client, headers, ws, _conv=None):
    """A fresh conversation each time, so B4's one-active-run lock never answers first."""
    conv = await client.post(f"/api/v1/workspaces/{ws}/conversations", json={}, headers=headers)
    return await client.post(
        f"/api/v1/workspaces/{ws}/conversations/{conv.json()['id']}/messages",
        json={"content": "hi"},
        headers=headers,
    )


async def _member(client: AsyncClient, owner, ws, email: str):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "M"},
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    await client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"email": email, "role": "member"},
        headers=owner,
    )
    conv = await client.post(f"/api/v1/workspaces/{ws}/conversations", json={}, headers=headers)
    return headers, uuid.UUID(conv.json()["id"])


async def test_the_request_past_the_limit_gets_429_and_headers_are_always_set(
    client: AsyncClient,
) -> None:
    headers, ws, conv = await register_workspace_and_conversation(client, "rl-one@example.com")
    for remaining in (2, 1, 0):
        resp = await _send(client, headers, ws, conv)
        assert resp.status_code == 202, resp.text
        assert resp.headers["RateLimit-Limit"] == "3"
        assert resp.headers["RateLimit-Remaining"] == str(remaining)

    resp = await _send(client, headers, ws, conv)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0


async def test_users_have_separate_windows_and_share_the_workspace_one(
    client: AsyncClient,
) -> None:
    owner, ws, conv = await register_workspace_and_conversation(client, "rl-a@example.com")
    other, other_conv = await _member(client, owner, ws, "rl-b@example.com")
    for _ in range(3):
        assert (await _send(client, owner, ws, conv)).status_code == 202
    assert (await _send(client, owner, ws, conv)).status_code == 429  # owner's own window

    assert (await _send(client, other, ws, other_conv)).status_code == 202
    # 4 counted in the workspace window (the 429 counts too), so this is the 6th > 5.
    assert (await _send(client, other, ws, other_conv)).status_code == 429


async def test_a_redis_outage_leaves_the_route_working(client: AsyncClient) -> None:
    headers, ws, conv = await register_workspace_and_conversation(client, "rl-down@example.com")

    class _DownRedis:
        async def incr(self, *_: object) -> int:
            raise ConnectionError("redis is down")

        async def get(self, *_: object) -> None:
            raise ConnectionError("redis is down")

        async def set(self, *_: object, **__: object) -> None:
            raise ConnectionError("redis is down")

    previous = app.dependency_overrides[get_redis]
    app.dependency_overrides[get_redis] = lambda: _DownRedis()
    try:
        resp = await _send(client, headers, ws, conv)
    finally:
        app.dependency_overrides[get_redis] = previous
    assert resp.status_code == 202, resp.text
    assert "RateLimit-Limit" not in resp.headers
