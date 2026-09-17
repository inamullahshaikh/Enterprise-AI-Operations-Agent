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
from langgraph.types import Command
from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from relay_core.agent.deps import AgentDeps, MemoryDispatcher
from relay_core.agent.graph import compile_graph
from relay_core.agent.state import AgentState
from relay_core.config import Settings
from relay_core.connectors.breaker import CircuitBreaker
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.approvals import ApprovalRepository
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.llm_calls import LLMCallRepository
from relay_core.db.repositories.memories import MemoryRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.db.repositories.policies import WorkspacePolicyRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository
from relay_core.events.publisher import EventPublisher
from relay_core.events.types import RUN_FAILED, RUN_STARTED
from relay_core.llm.gateway import LLMGateway, build_llm_gateway
from relay_core.security.crypto import build_kms
from relay_core.storage.object_store import ObjectStore, build_object_store
from relay_core.tools.executor import ToolExecutor
from relay_core.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def after_commit_dispatcher(session: AsyncSession) -> MemoryDispatcher:
    """Enqueues `relay_worker.tasks.memory.extract_memories` once the run's own transaction has
    committed (docs/system-design.md section 12.2).

    The wait matters: `finalize` runs *inside* that transaction, so a task enqueued there could
    be picked up by another worker before the `agent_runs` row says `completed` — and extraction
    refuses to run on anything that isn't. SQLAlchemy's `after_commit` event is the hook that
    already exists for this; nothing here needs an outbox table.

    A failure to enqueue is logged and swallowed. The run has already succeeded by then, and the
    cost of a missing enqueue is one missing memory.
    """

    async def _dispatch(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
        @event.listens_for(session.sync_session, "after_commit", once=True)
        def _enqueue(_session: Session) -> None:
            try:
                from relay_worker.tasks.memory import run_extract_memories

                run_extract_memories.delay(str(workspace_id), str(run_id))
            except Exception:  # noqa: BLE001 - the answer is already the user's
                logger.exception("could not enqueue memory extraction for run %s", run_id)

    return _dispatch


def build_agent_deps(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    gateway: LLMGateway | None = None,
    object_store: ObjectStore | None = None,
    extract_memories: MemoryDispatcher | None = None,
) -> AgentDeps:
    """Shared by the first run of a turn and by every later resume of it, so a run that comes
    back from `awaiting_approval` is rebuilt with exactly the same wiring it was suspended
    with."""
    kms = build_kms(settings)
    resolved_gateway = gateway or build_llm_gateway(session, redis, settings)
    tool_calls = ToolCallRepository(session)
    events = EventPublisher(redis)
    breaker = CircuitBreaker(redis)
    installations = ConnectorInstallationRepository(session)
    tool_registry = ToolRegistry(
        session,
        object_store or build_object_store(settings),
        kms,
        resolved_gateway,
        settings,
        breaker=breaker,
    )
    return AgentDeps(
        gateway=resolved_gateway,
        events=events,
        settings=settings,
        conversations=ConversationRepository(session),
        messages=MessageRepository(session),
        runs=AgentRunRepository(session),
        llm_calls=LLMCallRepository(session),
        tool_calls=tool_calls,
        tool_definitions=ToolDefinitionRepository(session),
        attachments=AttachmentRepository(session),
        documents=DocumentRepository(session),
        tool_registry=tool_registry,
        tool_executor=ToolExecutor(tool_calls, events, breaker, installations),
        approvals=ApprovalRepository(session),
        policies=WorkspacePolicyRepository(session),
        members=WorkspaceMemberRepository(session),
        memories=MemoryRepository(session),
        breaker=breaker,
        extract_memories=extract_memories or after_commit_dispatcher(session),
    )


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
    extract_memories: MemoryDispatcher | None = None,
) -> None:
    deps = build_agent_deps(
        session,
        redis,
        settings,
        gateway=gateway,
        object_store=object_store,
        extract_memories=extract_memories,
    )
    runs = deps.runs
    messages = deps.messages
    events = deps.events

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


async def resume_agent_once(
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    decision: dict[str, Any],
    *,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[Any],
    gateway: LLMGateway | None = None,
    object_store: ObjectStore | None = None,
    extract_memories: MemoryDispatcher | None = None,
) -> None:
    """Restarts a run parked at `approval_gate`'s `interrupt()` (docs/system-design.md section
    13.2). `Command(resume=...)` re-enters that node with `decision` as the return value of the
    `interrupt()` call; everything before it in the node re-executes, which is why nothing there
    has side effects worth repeating.

    No initial state is passed — LangGraph reconstructs it from the checkpoint under the same
    `thread_id`, which is what makes this work from a different worker process than the one that
    suspended the run.

    The status guard is the second half of the double-decision defence: `ApprovalRepository.
    decide` refuses to overwrite a decision, and this refuses to resume a run that isn't parked,
    so neither a retried Celery task nor a double-submitted decision replays an approved write.
    """
    deps = build_agent_deps(
        session,
        redis,
        settings,
        gateway=gateway,
        object_store=object_store,
        extract_memories=extract_memories,
    )
    run = await deps.runs.get(workspace_id, run_id)
    if run is None:
        logger.error(
            "resume_agent_once: no agent_runs row %s in workspace %s", run_id, workspace_id
        )
        return
    if run.status != "awaiting_approval":
        logger.warning(
            "resume_agent_once: run %s is %s, not awaiting_approval - ignoring", run_id, run.status
        )
        return

    graph = compile_graph(deps, checkpointer)
    try:
        await graph.ainvoke(
            Command(resume=decision),
            config={
                "configurable": {"thread_id": str(run.conversation_id)},
                "metadata": {"run_id": str(run_id)},
            },
        )
    except Exception as exc:  # noqa: BLE001 - this is the top-level run boundary
        logger.exception("agent run %s failed on resume", run_id)
        await deps.runs.mark_failed(
            workspace_id, run_id, error_code="internal_error", error_message=str(exc)
        )
        await deps.events.publish(run_id, RUN_FAILED, {"error": str(exc)})
