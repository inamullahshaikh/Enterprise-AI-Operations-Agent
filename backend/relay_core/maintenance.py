"""The run watchdog and the retention sweep (docs/system-design.md sections 19.1, 14.5).

Both are system jobs with no requesting user, so like `relay_core.approvals` they read across
workspaces once, then act on each row through its own `workspace_id`. They live here rather than
in `relay_worker.tasks.maintenance` so tests can import them without the Celery app.
"""

import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.attachments import Attachment
from relay_core.db.models.conversations import Conversation
from relay_core.db.models.llm import LLMCall
from relay_core.db.models.policies import WorkspacePolicy
from relay_core.db.models.runs import AgentRun
from relay_core.db.models.tool_calls import ToolCall
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.audit import AuditLogRepository
from relay_core.events.publisher import EventPublisher
from relay_core.events.types import RUN_FAILED

logger = logging.getLogger(__name__)

STALL_AFTER = timedelta(minutes=10)
# The UI offers a retry for this code; the run did nothing wrong, its worker went away.
STALLED_ERROR_CODE = "stalled"


async def fail_stalled_runs(session: AsyncSession, events: EventPublisher, *, now: datetime) -> int:
    """Fails every `running` run with no LLM call and no tool call started in `STALL_AFTER`.

    Database timestamps are the authority, not the Redis event stream: the stream expires after
    an hour, and a run can be silent on it for the whole of one slow tool call, whose
    `tool_calls.started_at` is the honest signal that it is still alive. `awaiting_approval` is
    not touched; `expire_stale_approvals` owns that, on its own 24-hour window."""
    cutoff = now - STALL_AFTER
    recent_llm = exists().where(LLMCall.run_id == AgentRun.id, LLMCall.created_at >= cutoff)
    recent_tool = exists().where(
        ToolCall.run_id == AgentRun.id,
        func.coalesce(ToolCall.started_at, ToolCall.created_at) >= cutoff,
    )
    stmt = (
        select(AgentRun.workspace_id, AgentRun.id)
        .where(
            AgentRun.status == "running",
            func.coalesce(AgentRun.started_at, AgentRun.created_at) < cutoff,
            ~recent_llm,
            ~recent_tool,
        )
        .limit(200)
    )
    stalled = (await session.execute(stmt)).all()
    runs = AgentRunRepository(session)
    for workspace_id, run_id in stalled:
        message = f"No progress for {int(STALL_AFTER.total_seconds() // 60)} minutes"
        await runs.mark_failed(
            workspace_id, run_id, error_code=STALLED_ERROR_CODE, error_message=message
        )
        await events.publish(run_id, RUN_FAILED, {"error": message, "retryable": True})
    return len(stalled)


class BlobDeleter(Protocol):
    async def delete(self, key: str) -> None: ...


class ThreadDeleter(Protocol):
    async def adelete_thread(self, thread_id: str) -> None: ...


@dataclass
class RetentionCounts:
    tool_outputs_cleared: int = 0
    attachments_deleted: int = 0
    checkpoints_deleted: int = 0


async def apply_retention(
    session: AsyncSession,
    object_store: BlobDeleter,
    checkpointer: ThreadDeleter,
    *,
    now: datetime,
) -> dict[uuid.UUID, RetentionCounts | str]:
    """Per workspace, deletes what is older than `data_retention_days`: tool outputs (the
    `tool_calls` rows stay, they are the audit trail), attachment blobs and rows, and LangGraph
    checkpoints of conversations untouched since the cutoff. `llm_calls` and `audit_logs` are kept
    (section 14.5). A workspace with `data_retention_days = 0` is skipped: 0 reads as "not set",
    and deleting everything is not what anyone means by it.

    Each workspace runs in its own savepoint and commits on its own, so one failure is logged and
    recorded in the result without stopping the sweep. Returns counts, or the error, per
    workspace."""
    policies = (
        await session.execute(
            select(WorkspacePolicy.workspace_id, WorkspacePolicy.data_retention_days).where(
                WorkspacePolicy.data_retention_days > 0
            )
        )
    ).all()
    results: dict[uuid.UUID, RetentionCounts | str] = {}
    for workspace_id, days in policies:
        try:
            async with session.begin_nested():
                counts = await _retain_workspace(
                    session, object_store, checkpointer, workspace_id, now - timedelta(days=days)
                )
                await AuditLogRepository(session).record(
                    workspace_id,
                    actor_type="system",
                    actor_user_id=None,
                    action="retention.applied",
                    target_type="workspace",
                    target_id=workspace_id,
                    details={"retention_days": days, **asdict(counts)},
                )
            await session.commit()
            results[workspace_id] = counts
        except Exception as exc:  # noqa: BLE001 - one workspace must not stop the sweep
            logger.exception("retention failed for workspace %s", workspace_id)
            results[workspace_id] = str(exc)
    return results


async def _retain_workspace(
    session: AsyncSession,
    object_store: BlobDeleter,
    checkpointer: ThreadDeleter,
    workspace_id: uuid.UUID,
    cutoff: datetime,
) -> RetentionCounts:
    counts = RetentionCounts()
    cleared: Any = await session.execute(
        update(ToolCall)
        .where(
            ToolCall.workspace_id == workspace_id,
            ToolCall.created_at < cutoff,
            ToolCall.output.is_not(None),
        )
        .values(output=None)
    )
    counts.tool_outputs_cleared = cleared.rowcount

    old = (
        await session.execute(
            select(Attachment.id, Attachment.blob_key).where(
                Attachment.workspace_id == workspace_id, Attachment.created_at < cutoff
            )
        )
    ).all()
    for _, blob_key in old:
        await object_store.delete(blob_key)
    if old:
        await session.execute(
            delete(Attachment).where(
                Attachment.workspace_id == workspace_id, Attachment.id.in_([i for i, _ in old])
            )
        )
    counts.attachments_deleted = len(old)

    idle = (
        await session.execute(
            select(Conversation.id).where(
                Conversation.workspace_id == workspace_id, Conversation.updated_at < cutoff
            )
        )
    ).scalars()
    for conversation_id in idle:
        # The graph's thread id is the conversation id (`relay_core.agent.runner`).
        await checkpointer.adelete_thread(str(conversation_id))
        counts.checkpoints_deleted += 1
    return counts
