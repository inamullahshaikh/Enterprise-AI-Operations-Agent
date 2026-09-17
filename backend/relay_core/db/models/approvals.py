"""Human-in-the-loop approvals (docs/system-design.md sections 13, 14.3), Phase 5's addition to
the schema.

One row can gate several tool calls at once: `tool_call_ids` is an array because the executor
groups similar write calls into a single approval ("create 12 Gmail drafts") rather than asking
twelve separate times (section 13.4). `partially_approved` is where a per-item decision lands
when the approver ticks some items and not others. `proposed_args` keeps what the model
originally asked for and `final_args` records what was actually executed after any edits, so the
audit trail shows both rather than overwriting the model's request with the human's correction.

There is deliberately no `tool_calls.approval_id` back-pointer: the link lives here, in
`tool_call_ids`, because a batch approval points at many calls and the reverse column would only
be able to represent one of them.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, WorkspaceScoped, uuid7

STATUSES = ("pending", "approved", "partially_approved", "rejected", "expired")

# Section 13.4: an undecided approval doesn't pin a run open forever.
DEFAULT_EXPIRY_HOURS = 24


class Approval(Base, WorkspaceScoped):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    tool_call_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_args: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    final_args: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    required_role: Mapped[str] = mapped_column(
        String, nullable=False, default="member", server_default="member"
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','partially_approved','rejected','expired')",
            name="ck_approvals_status",
        ),
        # Partial index: the approvals inbox and the expiry watchdog both only ever scan
        # undecided rows, and decided ones accumulate indefinitely.
        Index(
            "ix_approvals_pending",
            "workspace_id",
            "status",
            postgresql_where=text("status = 'pending'"),
        ),
    )
