"""The transport-agnostic core of running one agent turn (docs/system-design.md
section 4.3, steps 4-8): loads the triggering message, builds the graph, and
invokes it. Called by both the Celery task (`relay_worker.tasks.agent.run_agent`)
and directly by integration tests (via the `get_run_dispatcher` override in
`relay_api/deps.py`) so the whole flow is testable without a real Celery worker.

`checkpointer`, `gateway`, and `object_store` are all injected rather than constructed inline:
the Celery task passes the real `AsyncPostgresSaver`/a real Gemini-backed gateway/a real
R2-backed store, while tests pass an in-memory `MemorySaver`, a gateway built on a scripted fake
client, and (for the `file_upload` connector) an in-memory fake object store — the same seam
`test_debug_gemini_ping.py` already uses via `get_genai_client`, just reachable here without
going through FastAPI's DI.
"""

import logging
import uuid
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.agent.deps import AgentDeps
from relay_core.agent.graph import compile_graph
from relay_core.agent.state import AgentState
from relay_core.config import Settings
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.llm_calls import LLMCallRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.events.publisher import EventPublisher
from relay_core.events.types import RUN_FAILED, RUN_STARTED
from relay_core.llm.gateway import LLMGateway, build_llm_gateway
from relay_core.security.crypto import build_kms
from relay_core.storage.object_store import ObjectStore, build_object_store
from relay_core.tools.executor import ToolExecutor
from relay_core.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


async def run_agent_once(
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[Any],
    gateway: LLMGateway | None = None,
    object_store: ObjectStore | None = None,
) -> None:
    runs = AgentRunRepository(session)
    messages = MessageRepository(session)
    conversations = ConversationRepository(session)
    llm_calls = LLMCallRepository(session)
    tool_calls = ToolCallRepository(session)
    connector_installations = ConnectorInstallationRepository(session)
    attachments = AttachmentRepository(session)
    events = EventPublisher(redis)
    kms = build_kms(settings)
    tool_registry = ToolRegistry(session, object_store or build_object_store(settings), kms)

    run = await runs.get(workspace_id, run_id)
    if run is None:
        logger.error("run_agent_once: no agent_runs row %s in workspace %s", run_id, workspace_id)
        return
    trigger = (
        await messages.get(workspace_id, run.trigger_message_id) if run.trigger_message_id else None
    )
    if trigger is None:
        await runs.mark_failed(
            workspace_id,
            run_id,
            error_code="missing_trigger",
            error_message="Trigger message not found",
        )
        return

    await runs.mark_running(workspace_id, run_id)
    await events.publish(run_id, RUN_STARTED, {"run_id": str(run_id)})

    deps = AgentDeps(
        gateway=gateway or build_llm_gateway(session, redis, settings),
        events=events,
        settings=settings,
        conversations=conversations,
        messages=messages,
        runs=runs,
        llm_calls=llm_calls,
        tool_calls=tool_calls,
        connector_installations=connector_installations,
        attachments=attachments,
        tool_registry=tool_registry,
        tool_executor=ToolExecutor(tool_calls, events),
    )
    graph = compile_graph(deps, checkpointer)
    initial_state = AgentState(
        workspace_id=workspace_id,
        user_id=run.user_id,
        run_id=run_id,
        conversation_id=run.conversation_id,
        trigger_message_id=trigger.id,
        user_message=trigger.content,
    )

    try:
        await graph.ainvoke(
            initial_state,
            config={
                "configurable": {"thread_id": str(run.conversation_id)},
                "metadata": {"run_id": str(run_id)},
            },
        )
    except Exception as exc:  # noqa: BLE001 - this is the top-level run boundary
        logger.exception("agent run %s failed", run_id)
        await runs.mark_failed(
            workspace_id, run_id, error_code="internal_error", error_message=str(exc)
        )
        await events.publish(run_id, RUN_FAILED, {"error": str(exc)})
