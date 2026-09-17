import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from relay_core.db.models.approvals import DEFAULT_EXPIRY_HOURS, Approval
from relay_core.db.repositories.base import WorkspaceScopedRepository

DECIDED_STATUSES = ("approved", "partially_approved", "rejected", "expired")


class ApprovalRepository(WorkspaceScopedRepository[Approval]):
    model = Approval

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        tool_call_ids: list[uuid.UUID],
        summary: str,
        proposed_args: list[dict[str, Any]],
        requested_by: uuid.UUID,
        required_role: str = "member",
        expires_in_hours: int = DEFAULT_EXPIRY_HOURS,
    ) -> Approval:
        approval = Approval(
            workspace_id=workspace_id,
            run_id=run_id,
            tool_call_ids=tool_call_ids,
            summary=summary,
            proposed_args=proposed_args,
            requested_by=requested_by,
            required_role=required_role,
            expires_at=datetime.now(UTC) + timedelta(hours=expires_in_hours),
        )
        self.session.add(approval)
        await self.session.flush()
        return approval

    async def list_pending(self, workspace_id: uuid.UUID) -> list[Approval]:
        """Backs the approvals inbox (GET /workspaces/{ws}/approvals?status=pending). Ordered
        by `expires_at` so whatever is closest to timing out surfaces first."""
        stmt = (
            select(Approval)
            .where(Approval.workspace_id == workspace_id, Approval.status == "pending")
            .order_by(Approval.expires_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_expired_across_workspaces(
        self, *, now: datetime, limit: int = 200
    ) -> list[Approval]:
        """**A cross-tenant query** (the other is the tool-sync sweep's), deliberately: the expiry
        watchdog (`relay_worker.tasks.maintenance`) is a system sweep with no requesting user
        and no workspace to scope to, so the tenant filter every other method enforces has
        nothing to bind to here. The name says so loudly rather than hiding it behind an
        optional `workspace_id=None`.

        Every row it returns still carries its own `workspace_id`, and the caller feeds that
        back into the ordinary tenant-scoped methods — so the exemption stops at this one
        SELECT. Batched via `limit` so one sweep can't load an unbounded backlog.
        """
        stmt = (
            select(Approval)
            .where(Approval.status == "pending", Approval.expires_at <= now)
            .order_by(Approval.expires_at)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def decide(
        self,
        workspace_id: uuid.UUID,
        id_: uuid.UUID,
        *,
        status: str,
        decided_by: uuid.UUID | None,
        final_args: list[dict[str, Any]] | None = None,
        reason: str | None = None,
    ) -> Approval:
        """Records a terminal decision, refusing to overwrite one that already exists.

        The guard is what stops a double-submitted decision from resuming the same parked run
        twice — two resumes would replay the approved write against a checkpoint that no longer
        expects it. Callers surface the refusal as a conflict rather than retrying.
        """
        approval = await self.get(workspace_id, id_)
        if approval is None:
            raise ValueError(f"approvals row {id_} not found in workspace {workspace_id}")
        if approval.status != "pending":
            raise AlreadyDecidedError(
                f"approval {id_} is already {approval.status}", status=approval.status
            )
        approval.status = status
        approval.decided_by = decided_by
        approval.final_args = final_args
        approval.decision_reason = reason
        approval.decided_at = datetime.now(UTC)
        await self.session.flush()
        return approval


class AlreadyDecidedError(Exception):
    def __init__(self, message: str, *, status: str) -> None:
        super().__init__(message)
        self.status = status
