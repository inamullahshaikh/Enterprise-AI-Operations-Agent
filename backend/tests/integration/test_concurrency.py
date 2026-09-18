"""Phase 8 B4: per-workspace run permits and the one-active-run-per-conversation lock
(docs/system-design.md section 19.3)."""

import time
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_run_dispatcher
from relay_api.main import app
from relay_core.agent.concurrency import _key, acquire_permit, release_permit
from relay_core.db.repositories.agent_runs import AgentRunRepository
from tests.integration.scripted_model import register_workspace_and_conversation

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


async def test_a_fourth_run_waits_until_a_permit_frees(redis_client: Redis) -> None:
    ws = uuid.uuid4()
    runs = [uuid.uuid4() for _ in range(4)]
    assert [await acquire_permit(redis_client, ws, r, 3) for r in runs[:3]] == [True] * 3
    assert await acquire_permit(redis_client, ws, runs[3], 3) is False
    await release_permit(redis_client, ws, runs[0])
    assert await acquire_permit(redis_client, ws, runs[3], 3) is True


async def test_a_dead_holders_permit_is_reclaimed_after_its_ttl(redis_client: Redis) -> None:
    ws = uuid.uuid4()
    await redis_client.zadd(_key(ws), {"dead-run": time.time() - 1})  # lapsed permit
    assert await acquire_permit(redis_client, ws, uuid.uuid4(), 1) is True


async def test_a_second_message_while_a_run_is_active_gets_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    async def _noop(*_: object) -> None:
        return None

    app.dependency_overrides[get_run_dispatcher] = lambda: _noop
    try:
        headers, ws, conv = await register_workspace_and_conversation(client, "lock@example.com")
        url = f"/api/v1/workspaces/{ws}/conversations/{conv}/messages"
        first = await client.post(url, json={"content": "one"}, headers=headers)
        assert first.status_code == 202

        busy = await client.post(url, json={"content": "two"}, headers=headers)
        assert busy.status_code == 409
        assert first.json()["run_id"] in busy.json()["detail"]

        # Parked on an approval: still holds the lock.
        runs = AgentRunRepository(db_session)
        run_id = uuid.UUID(first.json()["run_id"])
        await runs.mark_awaiting_approval(ws, run_id)
        assert (await client.post(url, json={"content": "x"}, headers=headers)).status_code == 409

        await runs.mark_failed(ws, run_id, error_code="x", error_message="x")
        assert (await client.post(url, json={"content": "y"}, headers=headers)).status_code == 202
    finally:
        app.dependency_overrides.pop(get_run_dispatcher, None)
