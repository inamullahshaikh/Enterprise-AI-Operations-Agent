"""Tool call log (docs/system-design.md section 14.3), trimmed per docs/adr/0009: no
`run_steps`/`tool_definitions` FKs (`plan_step_id` is a plain string, matching the plan JSON's
`PlanStep.id`), no `idempotency_key`/`output_blob_key`/`attempt` columns (idempotent replay on
approval-resume is Phase 5, large-output S3 offload is Phase 4 — nothing writes those yet).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
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
    latency_ms: Mapped[int | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint("risk IN ('read','write','destructive')", name="ck_tool_calls_risk"),
        CheckConstraint("status IN ('running','succeeded','failed')", name="ck_tool_calls_status"),
    )
