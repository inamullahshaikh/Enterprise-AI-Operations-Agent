"""Approval expiry (docs/system-design.md section 13.4).

An undecided approval holds a run open and keeps its checkpoint alive, so once its window has
elapsed the whole thing is closed out. It cascades three ways: the approval becomes `expired`,
its proposed `tool_calls` rows become `skipped` (they were never executed and now never will
be), and the run becomes `expired` — terminal, unlike `awaiting_approval`, because its
checkpoint will never be resumed. The `approval.decided` event goes out last so a client still
listening on the run's stream learns why it stopped.

Lives in `relay_core` rather than in the Celery task that schedules it, for the same reason
`relay_core.agent.runner.run_agent_once` does: `relay_worker.app` reads settings at import time,
so anything importable from a test has to sit on this side of that line.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.approvals import ApprovalRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.events.publisher import EventPublisher
from relay_core.events.types import APPROVAL_DECIDED

_EXPIRY_REASON = "Nobody decided before the approval window elapsed"


async def expire_stale_approvals(session: AsyncSession, events: EventPublisher) -> int:
    """Sweeps every approval whose window has elapsed, returning how many were closed out."""
    approvals = ApprovalRepository(session)
    runs = AgentRunRepository(session)
    tool_calls = ToolCallRepository(session)

    stale = await approvals.list_expired_across_workspaces(now=datetime.now(UTC))
    for approval in stale:
        await approvals.decide(
            approval.workspace_id,
            approval.id,
            status="expired",
            decided_by=None,
            reason=_EXPIRY_REASON,
        )
        for row_id in approval.tool_call_ids:
            await tool_calls.mark_not_executed(
                approval.workspace_id, row_id, status="skipped", reason="Approval expired"
            )
        await runs.mark_expired(approval.workspace_id, approval.run_id)
        await events.publish(
            approval.run_id,
            APPROVAL_DECIDED,
            {"approval_id": str(approval.id), "status": "expired"},
        )
    return len(stale)
