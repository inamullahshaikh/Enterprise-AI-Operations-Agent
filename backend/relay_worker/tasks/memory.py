"""Celery entry point for post-run memory extraction (docs/system-design.md section 12.2), on
the `memory` queue that `relay_worker/app.py` has routed since Phase 2 and nothing used until
now. Thin for the same reason `tasks/ingest.py` is: the logic lives in
`relay_core.memory.extract`, so the task and the tests exercise one function.

`finalize` enqueues this *after* the run's transaction commits (see `relay_core.agent.runner.
after_commit_dispatcher`), so the `agent_runs` row this reads is the completed one, not the
running one the worker still had open.
"""

import asyncio
import logging
import uuid

from redis.asyncio import Redis

from relay_core.config import get_settings
from relay_core.db.session import get_sessionmaker
from relay_core.llm.gateway import build_llm_gateway
from relay_core.memory.extract import extract_memories
from relay_worker.app import app

logger = logging.getLogger(__name__)


@app.task(name="relay_worker.tasks.memory.extract_memories")  # type: ignore[untyped-decorator]
def run_extract_memories(workspace_id: str, run_id: str) -> None:
    asyncio.run(_run_async(uuid.UUID(workspace_id), uuid.UUID(run_id)))


async def _run_async(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        async with get_sessionmaker()() as session:
            gateway = build_llm_gateway(session, redis, settings)
            try:
                await extract_memories(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    session=session,
                    gateway=gateway,
                    settings=settings,
                )
                await session.commit()
            except Exception:
                # The user already has their answer; a failed extraction is a missing memory,
                # not a failed turn, and retrying it would re-run the whole LLM call for one.
                await session.rollback()
                logger.exception("Memory extraction failed for run %s", run_id)
    finally:
        await redis.aclose()
