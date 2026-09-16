"""LLM usage & pricing tables (docs/system-design.md section 14.3).

`langfuse_span_id` from the original design is dropped per
docs/adr/0008-cut-observability-stack.md; `llm_calls` and `agent_runs.cost_usd`
are the source of truth for cost/usage instead of an external trace viewer.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, WorkspaceScoped, uuid7


class LLMCall(Base, WorkspaceScoped):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    # The FK is added by the Phase 2 migration's trailing ALTER (agent_runs is
    # created after this table); declaring it here now that agent_runs exists in
    # the ORM metadata is safe because Alembic — not this declaration — controls
    # DDL ordering.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    node: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    fallback_from: Mapped[str | None] = mapped_column(String)
    thinking_level: Mapped[str | None] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    cached_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    thought_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=0, server_default="0"
    )
    latency_ms: Mapped[int] = mapped_column(nullable=False)
    finish_reason: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint("status IN ('ok','error','blocked')", name="ck_llm_calls_status"),
    )


class ModelPricing(Base):
    """Per-model USD pricing, read at cost-calculation time (docs/system-design.md
    section 9.1: "Pricing lives in a config table, not code, because prices change").
    """

    __tablename__ = "model_pricing"

    model: Mapped[str] = mapped_column(String, primary_key=True)
    input_per_mtok: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    output_per_mtok: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    cached_input_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")
