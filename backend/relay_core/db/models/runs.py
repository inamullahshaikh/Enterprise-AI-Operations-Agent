"""Agent runs (docs/system-design.md sections 14.3, 14.4).

`capability_snapshot` and `tool_calls` land in Phase 3 now that the capability resolver and
tool executor exist to populate them (`relay_core.agent.nodes.load_context`,
`relay_core.tools.executor`). `eval_run_id` is still deferred — the Phase 3 eval harness has
no `eval_runs` table yet (docs/adr/0009). `status` and `route` only allow the values this
phase's graph can actually produce; later phases extend the CHECK constraints (Phase 5's
approvals added `awaiting_approval`/`expired`, budgets add `budget_exceeded`, cancellation adds
`cancelled`).

`awaiting_approval` means the graph hit `interrupt()` in `approval_gate` and the worker exited —
the run is parked on a checkpoint until someone decides, and `expired` is where the watchdog
(`relay_worker.tasks.maintenance`) leaves it if nobody does within
`Approval.DEFAULT_EXPIRY_HOURS`.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, WorkspaceScoped, uuid7


class AgentRun(Base, WorkspaceScoped):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    trigger_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id")
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    route: Mapped[str | None] = mapped_column(String)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    missing_capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # What `load_context` resolved as available at run start (relay_core.capabilities.resolver) —
    # a snapshot, not a live view, since installations can change mid-run.
    capability_snapshot: Mapped[list[str] | None] = mapped_column(JSONB)
    final_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id")
    )
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(Text)
    llm_calls: Mapped[int] = mapped_column(default=0, server_default="0")
    tool_calls: Mapped[int] = mapped_column(default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    thought_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=0, server_default="0"
    )
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','awaiting_approval','awaiting_input',"
            "'completed','failed','expired')",
            name="ck_agent_runs_status",
        ),
        CheckConstraint("route IN ('direct','task','blocked')", name="ck_agent_runs_route"),
    )
