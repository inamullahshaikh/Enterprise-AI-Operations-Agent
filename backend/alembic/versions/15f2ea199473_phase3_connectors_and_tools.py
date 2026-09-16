"""Phase 3: connector installations/credentials, attachments, tool_calls

Revision ID: 15f2ea199473
Revises: b3d8a1f6c92e
Create Date: 2026-09-16

Covers the subset of docs/system-design.md section 14.3 needed for Phase 3
(docs/system-design.md section 28, "Phase 3 - Connector framework + first connectors +
eval harness"), trimmed per docs/adr/0009-phase3-connector-metadata-in-code.md:
`connector_definitions`, `tool_definitions`, `capability_bindings`, and `run_steps` are not
created — see that ADR for why. `agent_runs` gains `capability_snapshot` and `tool_calls`,
deferred from the Phase 2 migration until something populates them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "15f2ea199473"
down_revision: str | None = "b3d8a1f6c92e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("capability_snapshot", postgresql.JSONB()))
    op.add_column(
        "agent_runs",
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "connector_installations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("health", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("health_message", sa.Text()),
        sa.Column("last_health_at", sa.DateTime(timezone=True)),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="100"),
        sa.Column(
            "installed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("workspace_id", "slug", name="uq_installations_ws_slug"),
        sa.CheckConstraint(
            "status IN ('pending','active','disabled','error')", name="ck_installations_status"
        ),
        sa.CheckConstraint(
            "health IN ('unknown','healthy','degraded','down')", name="ck_installations_health"
        ),
    )
    op.create_index("ix_installations_ws", "connector_installations", ["workspace_id", "status"])

    op.create_table(
        "connector_credentials",
        sa.Column(
            "installation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connector_installations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_dek", sa.LargeBinary(), nullable=False),
        sa.Column("kms_key_id", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_credentials_ws", "connector_credentials", ["workspace_id"])

    op.create_table(
        "attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("blob_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("profile", postgresql.JSONB()),
        sa.Column(
            "inferred_capabilities",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('table','document','image','other')", name="ck_attachments_kind"
        ),
    )
    op.create_index("ix_attachments_conv", "attachments", ["conversation_id", "created_at"])

    op.create_table(
        "tool_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan_step_id", sa.String(), nullable=False),
        sa.Column(
            "installation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connector_installations.id", ondelete="SET NULL"),
        ),
        sa.Column("llm_name", sa.String(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("risk", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("output", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("risk IN ('read','write','destructive')", name="ck_tool_calls_risk"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed')", name="ck_tool_calls_status"
        ),
    )
    op.create_index("ix_tool_calls_run", "tool_calls", ["run_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_tool_calls_run", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_attachments_conv", table_name="attachments")
    op.drop_table("attachments")
    op.drop_index("ix_credentials_ws", table_name="connector_credentials")
    op.drop_table("connector_credentials")
    op.drop_index("ix_installations_ws", table_name="connector_installations")
    op.drop_table("connector_installations")
    op.drop_column("agent_runs", "tool_calls")
    op.drop_column("agent_runs", "capability_snapshot")
