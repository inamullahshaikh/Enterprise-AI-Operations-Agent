"""Phase 5: workspace policies, approvals, and idempotent write replay

Revision ID: c7a41b9e5d20
Revises: 3731820176cc
Create Date: 2026-09-17

docs/system-design.md sections 13 (human-in-the-loop approvals) and 14.3.

Three things beyond the two new tables:

* `agent_runs.status` gains `awaiting_approval` (the graph hit `interrupt()` and the worker
  exited) and `expired` (nobody decided in time). `ix_runs_status`'s partial predicate is
  widened to match, since finding parked runs is exactly what the resume and watchdog paths do.
* `tool_calls.status` gains `pending_approval`/`rejected`/`skipped`, and `idempotency_key`
  arrives with its unique partial index — the database-level guarantee that a crashed-then-
  resumed worker cannot send the same email twice (section 13.3).
* Every existing workspace is backfilled with a default policy row, so the policy engine can
  assume one exists rather than carrying a "no row yet" branch.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7a41b9e5d20"
down_revision: str | None = "3731820176cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_RUN_BUDGET = (
    '{"max_steps":10,"max_tool_calls":40,"max_llm_calls":60,'
    '"max_cost_usd":0.5,"max_wall_seconds":300}'
)
_DEFAULT_APPROVAL_RULES = '{"default_write":"always","overrides":[]}'

_OLD_RUN_STATUSES = "status IN ('queued','running','awaiting_input','completed','failed')"
_NEW_RUN_STATUSES = (
    "status IN ('queued','running','awaiting_approval','awaiting_input',"
    "'completed','failed','expired')"
)
_OLD_TOOL_CALL_STATUSES = "status IN ('running','succeeded','failed')"
_NEW_TOOL_CALL_STATUSES = (
    "status IN ('pending_approval','running','succeeded','failed','rejected','skipped')"
)


def upgrade() -> None:
    op.create_table(
        "workspace_policies",
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "run_budget", postgresql.JSONB(), nullable=False, server_default=_DEFAULT_RUN_BUDGET
        ),
        sa.Column(
            "approval_rules",
            postgresql.JSONB(),
            nullable=False,
            server_default=_DEFAULT_APPROVAL_RULES,
        ),
        sa.Column(
            "email_domain_allow",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("allow_web_grounding", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("pii_redaction", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("data_retention_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(
        "INSERT INTO workspace_policies (workspace_id) SELECT id FROM workspaces "
        "ON CONFLICT (workspace_id) DO NOTHING"
    )

    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tool_call_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("proposed_args", postgresql.JSONB(), nullable=False),
        sa.Column("final_args", postgresql.JSONB()),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("required_role", sa.String(), nullable=False, server_default="member"),
        sa.Column(
            "requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("decision_reason", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','partially_approved','rejected','expired')",
            name="ck_approvals_status",
        ),
    )
    op.create_index("ix_approvals_workspace_id", "approvals", ["workspace_id"])
    op.create_index(
        "ix_approvals_pending",
        "approvals",
        ["workspace_id", "status"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint("ck_agent_runs_status", "agent_runs", _NEW_RUN_STATUSES)
    op.drop_index("ix_runs_status", table_name="agent_runs")
    op.create_index(
        "ix_runs_status",
        "agent_runs",
        ["status"],
        postgresql_where=sa.text("status IN ('queued','running','awaiting_approval')"),
    )

    op.add_column("tool_calls", sa.Column("idempotency_key", sa.String()))
    op.drop_constraint("ck_tool_calls_status", "tool_calls", type_="check")
    op.create_check_constraint("ck_tool_calls_status", "tool_calls", _NEW_TOOL_CALL_STATUSES)
    op.create_index(
        "ux_tool_calls_idem",
        "tool_calls",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL AND status = 'succeeded'"),
    )


def downgrade() -> None:
    op.drop_index("ux_tool_calls_idem", table_name="tool_calls")
    op.drop_constraint("ck_tool_calls_status", "tool_calls", type_="check")
    op.create_check_constraint("ck_tool_calls_status", "tool_calls", _OLD_TOOL_CALL_STATUSES)
    op.drop_column("tool_calls", "idempotency_key")

    op.drop_index("ix_runs_status", table_name="agent_runs")
    op.create_index(
        "ix_runs_status",
        "agent_runs",
        ["status"],
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint("ck_agent_runs_status", "agent_runs", _OLD_RUN_STATUSES)

    op.drop_index("ix_approvals_pending", table_name="approvals")
    op.drop_index("ix_approvals_workspace_id", table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("workspace_policies")
