"""Celery entry point for running one agent turn (docs/system-design.md section
4.3). Thin on purpose: all the actual logic lives in
`relay_core.agent.runner.run_agent_once`, which this just wires up with a
worker-owned session/Redis client/checkpointer, so the same core function is
what both this task and the integration tests exercise.
"""

import asyncio
import uuid

from redis.asyncio import Redis

from relay_core.agent.graph import get_postgres_checkpointer
from relay_core.agent.runner import run_agent_once
from relay_core.config import get_settings
from relay_core.db.session import get_sessionmaker
from relay_worker.app import app


@app.task(name="relay_worker.tasks.agent.run_agent")  # type: ignore[untyped-decorator]
def run_agent(workspace_id: str, run_id: str) -> None:
    asyncio.run(_run_agent_async(uuid.UUID(workspace_id), uuid.UUID(run_id)))


async def _run_agent_async(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        checkpointer = await get_postgres_checkpointer(settings)
        async with get_sessionmaker()() as session:
            await run_agent_once(
                workspace_id,
                run_id,
                session=session,
                redis=redis,
                settings=settings,
                checkpointer=checkpointer,
            )
            await session.commit()
    finally:
        await redis.aclose()
