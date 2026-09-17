"""Tool call log (docs/system-design.md section 14.3), trimmed per docs/adr/0009: no
`run_steps`/`tool_definitions` FKs (`plan_step_id` is a plain string, matching the plan JSON's
`PlanStep.id`), and no `output_blob_key`/`attempt` columns (nothing writes those yet).

`idempotency_key` arrives with Phase 5's approvals (section 13.3). It is
`sha256(approval_id + tool_call_id)`, set only on approved write calls, and it is what stops a
worker that died between executing a send and committing its checkpoint from sending twice on
resume. The `ux_tool_calls_idem` unique index enforces that at the database level rather than
trusting the executor's pre-flight check to win every race: it covers only `succeeded` rows, so
a failed attempt can still be retried under the same key.

`status` gains `pending_approval` (row written when the approval is created, before anyone has
decided), `rejected`, and `skipped` (an item the approver unticked in a batch).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, WorkspaceScoped, uuid7


class ToolCall(Base, WorkspaceScoped):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    plan_step_id: Mapped[str] = mapped_column(String, nullable=False)
    # Nullable: `file_upload` tool calls have no `connector_installations` row to point at
    # (relay_core.capabilities.resolver / relay_core.tools.registry docstrings).
    installation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_installations.id", ondelete="SET NULL")
    )
    llm_name: Mapped[str] = mapped_column(String, nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    risk: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String)
    latency_ms: Mapped[int | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint("risk IN ('read','write','destructive')", name="ck_tool_calls_risk"),
        CheckConstraint(
            "status IN ('pending_approval','running','succeeded','failed','rejected','skipped')",
            name="ck_tool_calls_status",
        ),
        Index(
            "ux_tool_calls_idem",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL AND status = 'succeeded'"),
        ),
    )
