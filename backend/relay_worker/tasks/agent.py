"""Celery entry points for running one agent turn, and for resuming one that stopped for a
human (docs/system-design.md sections 4.3, 13.2). Thin on purpose: all the actual logic lives
in `relay_core.agent.runner`, which these just wire up with a worker-owned session/Redis
client/checkpointer, so the same core functions are what both these tasks and the integration
tests exercise.
"""

import asyncio
import uuid
from typing import Any

from redis.asyncio import Redis

from relay_core.agent.concurrency import acquire_permit, release_permit
from relay_core.agent.graph import get_postgres_checkpointer
from relay_core.agent.runner import resume_agent_once, run_agent_once
from relay_core.config import get_settings
from relay_core.db.session import get_sessionmaker
from relay_worker.app import app


class _NoPermit(Exception):
    """The workspace is at `MAX_CONCURRENT_RUNS_PER_WORKSPACE`; the task retries later."""


def _retry_later(task: Any, exc: _NoPermit) -> None:
    # 5s, 10s, 20s ... capped at a minute. The run stays `queued` meanwhile (section 19.3).
    raise task.retry(exc=exc, countdown=min(60, 5 * 2**task.request.retries), max_retries=None)


@app.task(bind=True, name="relay_worker.tasks.agent.run_agent")  # type: ignore[untyped-decorator]
def run_agent(self: Any, workspace_id: str, run_id: str) -> None:
    try:
        asyncio.run(_run_agent_async(uuid.UUID(workspace_id), uuid.UUID(run_id)))
    except _NoPermit as exc:
        _retry_later(self, exc)


async def _run_agent_async(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    limit = settings.max_concurrent_runs_per_workspace
    if not await acquire_permit(redis, workspace_id, run_id, limit):
        await redis.aclose()
        raise _NoPermit
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
        await release_permit(redis, workspace_id, run_id)
        await redis.aclose()


@app.task(bind=True, name="relay_worker.tasks.agent.resume_agent")  # type: ignore[untyped-decorator]
def resume_agent(self: Any, workspace_id: str, run_id: str, decision: dict[str, Any]) -> None:
    try:
        asyncio.run(_resume_agent_async(uuid.UUID(workspace_id), uuid.UUID(run_id), decision))
    except _NoPermit as exc:
        _retry_later(self, exc)


async def _resume_agent_async(
    workspace_id: uuid.UUID, run_id: uuid.UUID, decision: dict[str, Any]
) -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    limit = settings.max_concurrent_runs_per_workspace
    if not await acquire_permit(redis, workspace_id, run_id, limit):
        await redis.aclose()
        raise _NoPermit
    try:
        checkpointer = await get_postgres_checkpointer(settings)
        async with get_sessionmaker()() as session:
            await resume_agent_once(
                workspace_id,
                run_id,
                decision,
                session=session,
                redis=redis,
                settings=settings,
                checkpointer=checkpointer,
            )
            await session.commit()
    finally:
        await release_permit(redis, workspace_id, run_id)
        await redis.aclose()
