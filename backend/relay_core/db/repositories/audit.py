import uuid
from contextvars import ContextVar
from typing import Any

from sqlalchemy import select, tuple_

from relay_core.db.models.audit import AuditLog
from relay_core.db.repositories.base import WorkspaceScopedRepository
from relay_core.security.scrub import scrub

# Set per request by the API's middleware, so no route has to thread `Request` through to
# `record`. Stays None in workers, where there is no client.
client_ip: ContextVar[str | None] = ContextVar("audit_client_ip", default=None)


class AuditLogRepository(WorkspaceScopedRepository[AuditLog]):
    model = AuditLog

    async def record(
        self,
        workspace_id: uuid.UUID,
        *,
        actor_type: str,
        actor_user_id: uuid.UUID | None,
        action: str,
        target_type: str,
        target_id: Any = None,
        run_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> AuditLog:
        """Writes one audit row. `details` is scrubbed here rather than trusted at each call
        site, so there is one place to fix when someone passes the wrong dict."""
        row = AuditLog(
            workspace_id=workspace_id,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            run_id=run_id,
            details=scrub(details or {}),
            ip=ip or client_ip.get(),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_for_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        action: str | None = None,
        actor_user_id: uuid.UUID | None = None,
        before: int | None = None,
        limit: int = 50,
    ) -> list[AuditLog]:
        """Newest first. `before` is the id of the last row of the previous page; keyset on
        (created_at, id) because this is the one table long enough for OFFSET to hurt."""
        stmt = select(AuditLog).where(AuditLog.workspace_id == workspace_id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if actor_user_id is not None:
            stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
        if before is not None:
            cursor = (
                await self.session.execute(
                    select(AuditLog.created_at, AuditLog.id).where(
                        AuditLog.workspace_id == workspace_id, AuditLog.id == before
                    )
                )
            ).one_or_none()
            if cursor is None:  # unknown, or another workspace's id
                return []
            stmt = stmt.where(tuple_(AuditLog.created_at, AuditLog.id) < tuple(cursor))
        stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())
