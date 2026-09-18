"""Audit trail for workspace admin visibility (docs/system-design.md §14.3, §15.5).

Append-only log of who did what, when. Indexed by time for efficient reverse-time queries.
Not deleted with the rest of a workspace's data (§14.5 keeps audit rows forever).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, WorkspaceScoped


class AuditLog(Base, WorkspaceScoped):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_type: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str | None] = mapped_column(String)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ip: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('user','agent','system','api_key')", name="ck_audit_logs_actor_type"
        ),
        Index(
            "ix_audit_logs_workspace_created",
            "workspace_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )
