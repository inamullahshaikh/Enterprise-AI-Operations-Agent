"""Celery entry point for document ingestion (docs/system-design.md section 11.1), on the
`ingest` queue that's been declared in `docker-compose.yml`/the Makefile since Phase 3 but
unused until now. Thin for the same reason `tasks/agent.py` is: the real logic lives in
`relay_core.rag.ingest.ingest_document`, which this just wires up with worker-owned
session/Redis/gateway/object-store, so the task and any test exercise the same function.
"""

import asyncio
import uuid

from redis.asyncio import Redis

from relay_core.config import get_settings
from relay_core.db.session import get_sessionmaker
from relay_core.llm.gateway import build_llm_gateway
from relay_core.rag.ingest import ingest_document
from relay_core.storage.object_store import build_object_store
from relay_worker.app import app


@app.task(name="relay_worker.tasks.ingest.ingest_document")  # type: ignore[untyped-decorator]
def run_ingest_document(workspace_id: str, document_id: str) -> None:
    asyncio.run(_run_async(uuid.UUID(workspace_id), uuid.UUID(document_id)))


async def _run_async(workspace_id: uuid.UUID, document_id: uuid.UUID) -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        async with get_sessionmaker()() as session:
            gateway = build_llm_gateway(session, redis, settings)
            object_store = build_object_store(settings)
            await ingest_document(
                workspace_id=workspace_id,
                document_id=document_id,
                session=session,
                object_store=object_store,
                gateway=gateway,
                settings=settings,
            )
            await session.commit()
    finally:
        await redis.aclose()
