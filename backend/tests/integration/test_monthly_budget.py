"""Phase 8 B2: a workspace past `monthly_budget_usd` is refused at enqueue with a 402 and no run
row (docs/system-design.md section 19.2)."""

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_run_dispatcher
from relay_api.main import app
from relay_core.db.models.llm import LLMCall
from relay_core.db.models.runs import AgentRun
from relay_core.policy.budgets import month_spend
from tests.integration.scripted_model import register_workspace_and_conversation

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture(autouse=True)
def _no_dispatch():
    async def _noop(*_: object) -> None:
        return None

    app.dependency_overrides[get_run_dispatcher] = lambda: _noop
    yield
    app.dependency_overrides.pop(get_run_dispatcher, None)


def _spend(ws: uuid.UUID, cost: str) -> LLMCall:
    return LLMCall(
        workspace_id=ws,
        node="plan",
        model="gemini-flash",
        cost_usd=Decimal(cost),
        latency_ms=1,
        status="ok",
    )


async def _send(client: AsyncClient, headers, ws, conv):
    return await client.post(
        f"/api/v1/workspaces/{ws}/conversations/{conv}/messages",
        json={"content": "hi"},
        headers=headers,
    )


async def _runs(db_session: AsyncSession, ws: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(AgentRun).where(AgentRun.workspace_id == ws)
    return int((await db_session.execute(stmt)).scalar_one())


async def test_at_cap_gets_402_and_no_run_then_raising_the_cap_unblocks(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers, ws, conv = await register_workspace_and_conversation(client, "cap@example.com")
    db_session.add(_spend(ws, "10.00"))  # the default cap is $10.00
    await db_session.flush()

    resp = await _send(client, headers, ws, conv)
    assert resp.status_code == 402, resp.text
    assert resp.headers["content-type"] == "application/problem+json"
    assert "resets on" in resp.json()["detail"]
    assert await _runs(db_session, ws) == 0

    resp = await client.patch(
        f"/api/v1/workspaces/{ws}", json={"monthly_budget_usd": "25.00"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert (await _send(client, headers, ws, conv)).status_code == 202
    assert await _runs(db_session, ws) == 1

    logs = await client.get(
        f"/api/v1/workspaces/{ws}/audit-logs",
        params={"action": "workspace.budget_changed"},
        headers=headers,
    )
    assert [r["details"] for r in logs.json()] == [{"from": "10.00", "to": "25.00"}]


async def test_under_cap_runs_and_other_workspaces_spend_does_not_count(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers, ws, conv = await register_workspace_and_conversation(client, "under@example.com")
    other = await client.post("/api/v1/workspaces", json={"name": "Other"}, headers=headers)
    db_session.add(_spend(uuid.UUID(other.json()["id"]), "50.00"))
    db_session.add(_spend(ws, "1.00"))
    await db_session.flush()

    assert (await _send(client, headers, ws, conv)).status_code == 202


async def test_only_the_owner_can_change_the_cap(client: AsyncClient) -> None:
    owner, ws, _ = await register_workspace_and_conversation(client, "cap-owner@example.com")
    admin_resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "cap-admin@example.com",
            "password": "correct horse battery staple",
            "full_name": "Admin",
        },
    )
    admin = {"Authorization": f"Bearer {admin_resp.json()['access_token']}"}
    await client.post(
        f"/api/v1/workspaces/{ws}/members",
        json={"email": "cap-admin@example.com", "role": "admin"},
        headers=owner,
    )
    resp = await client.patch(
        f"/api/v1/workspaces/{ws}", json={"monthly_budget_usd": "99"}, headers=admin
    )
    assert resp.status_code == 403


async def test_a_cached_under_budget_answer_is_dropped_when_invalidated(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    """`finalize` calls `invalidate_month_spend`; without it a workspace could keep spending for
    the cache's minute after crossing its cap."""
    from relay_core.policy.budgets import invalidate_month_spend

    _, ws, _ = await register_workspace_and_conversation(client, "cache@example.com")
    assert await month_spend(db_session, redis_client, ws) == 0
    db_session.add(_spend(ws, "12.00"))
    await db_session.flush()
    assert await month_spend(db_session, redis_client, ws) == 0  # still the cached answer
    await invalidate_month_spend(redis_client, ws)
    assert await month_spend(db_session, redis_client, ws) == Decimal("12.00")
