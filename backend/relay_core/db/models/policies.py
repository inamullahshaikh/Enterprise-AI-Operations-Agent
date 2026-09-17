"""Workspace governance settings (docs/system-design.md sections 13.1, 14.3, 19), Phase 5's
addition to the schema: one row per workspace, so the policy engine always has something to read
instead of hard-coded defaults scattered across call sites.

`run_budget` and `approval_rules` are `jsonb` rather than columns because their shapes are
per-tool and still growing (section 13.1's `always | never | over_threshold`, plus per-tool
overrides). The typed view of them — and all validation — lives in `relay_core.policy`; nothing
else should read these dicts directly.

Only `approval_rules` and `email_domain_allow` are honoured in Phase 5. The rest are stored now,
at their design defaults, so the phase that starts enforcing them needs no migration:
`run_budget` (Phase 8's budgets), `allow_web_grounding` (Phase 6's web search), `memory_enabled`
(Phase 7's memory), `pii_redaction` and `data_retention_days` (Phase 8's hardening).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base

# Mirrors `relay_core.agent.state.Budget`'s field defaults (section 8.3).
DEFAULT_RUN_BUDGET: dict[str, Any] = {
    "max_steps": 10,
    "max_tool_calls": 40,
    "max_llm_calls": 60,
    "max_cost_usd": 0.5,
    "max_wall_seconds": 300,
}

# Section 13.1's "Default policy: all write and destructive calls need approval."
DEFAULT_APPROVAL_RULES: dict[str, Any] = {"default_write": "always", "overrides": []}


class WorkspacePolicy(Base):
    """Keyed by `workspace_id` alone — one policy per workspace, so there's no separate `id`
    and no `WorkspaceScoped` mixin (which models a *column* on a multi-row tenant table)."""

    __tablename__ = "workspace_policies"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    run_budget: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: dict(DEFAULT_RUN_BUDGET)
    )
    approval_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: dict(DEFAULT_APPROVAL_RULES)
    )
    email_domain_allow: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )
    allow_web_grounding: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    pii_redaction: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    memory_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    data_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default="90"
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")
