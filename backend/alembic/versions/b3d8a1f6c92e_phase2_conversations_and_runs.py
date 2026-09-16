"""Phase 2: conversations, messages, and agent_runs

Revision ID: b3d8a1f6c92e
Revises: 896f48fce511
Create Date: 2026-09-16

Covers the subset of docs/system-design.md section 14.3 needed for Phase 2
(docs/system-design.md section 28, "Phase 2 - Agent core, no connectors"):
conversations, messages, agent_runs, plus the `llm_calls.run_id` FK deferred
from the Phase 1 migration. `run_steps`, `tool_calls`, and `approvals` are
Phase 3/5 tables and land with the migrations that introduce them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d8a1f6c92e"
down_revision: str | None = "896f48fce511"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("title", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("summary_upto_message_id", postgresql.UUID(as_uuid=True)),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_conversations_workspace_id", "conversations", ["workspace_id"])
    op.create_index(
        "ix_conversations_user", "conversations", ["workspace_id", "user_id", "last_message_at"]
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True)
        ),  # FK added below, once agent_runs exists
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_json", postgresql.JSONB()),
        sa.Column("token_count", sa.Integer()),
        sa.Column("feedback", sa.SmallInteger()),
        sa.Column("feedback_comment", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('user','assistant','system')", name="ck_messages_role"),
        sa.CheckConstraint("feedback IN (-1, 1)", name="ck_messages_feedback"),
    )
    op.create_index("ix_messages_workspace_id", "messages", ["workspace_id"])
    op.create_index("ix_messages_conv", "messages", ["conversation_id", "created_at"])

    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "trigger_message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id")
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("route", sa.String()),
        sa.Column("plan", postgresql.JSONB()),
        sa.Column("missing_capabilities", postgresql.JSONB()),
        sa.Column("final_message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id")),
        sa.Column("error_code", sa.String()),
        sa.Column("error_message", sa.Text()),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("thought_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','awaiting_input','completed','failed')",
            name="ck_agent_runs_status",
        ),
        sa.CheckConstraint("route IN ('direct','task','blocked')", name="ck_agent_runs_route"),
    )
    op.create_index("ix_runs_ws_created", "agent_runs", ["workspace_id", "created_at"])
    op.create_index(
        "ix_runs_status",
        "agent_runs",
        ["status"],
        postgresql_where=sa.text("status IN ('queued','running')"),
    )

    op.create_foreign_key(
        "fk_messages_run", "messages", "agent_runs", ["run_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_llm_calls_run", "llm_calls", "agent_runs", ["run_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    op.drop_constraint("fk_llm_calls_run", "llm_calls", type_="foreignkey")
    op.drop_constraint("fk_messages_run", "messages", type_="foreignkey")
    op.drop_index("ix_runs_status", table_name="agent_runs")
    op.drop_index("ix_runs_ws_created", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_messages_conv", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_user", table_name="conversations")
    op.drop_table("conversations")
