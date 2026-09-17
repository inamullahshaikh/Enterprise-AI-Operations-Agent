"""Phase 6: tool_definitions, connector_installations.last_synced_at

Revision ID: 4e8b2d71a9f3
Revises: c7a41b9e5d20
Create Date: 2026-09-17

docs/system-design.md sections 6.4 (discovery, schema-hash change detection), 6.7 (tool
registry), 14.3 (`tool_definitions`). Supersedes the `tool_definitions` part of
docs/adr/0009; `connector_definitions` and `capability_bindings` stay unbuilt.

No backfill here: rows come from calling each connector's `list_tools`, which is network I/O a
migration shouldn't do. The tool sync service fills them, including for installations that
predate this revision (`last_synced_at IS NULL`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4e8b2d71a9f3"
down_revision: str | None = "c7a41b9e5d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connector_installations",
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "tool_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "installation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connector_installations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("llm_name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(), nullable=False),
        sa.Column("schema_hash", sa.String(), nullable=False),
        sa.Column("risk", sa.String(), nullable=False),
        sa.Column("risk_overridden", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "capabilities", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"
        ),
        sa.Column("capability_source", sa.String(), nullable=False, server_default="declared"),
        sa.Column("tag_confidence", sa.REAL()),
        sa.Column("idempotent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("timeout_s", sa.REAL(), nullable=False, server_default="30"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("embedding", Vector(768)),
        sa.Column("embedding_model", sa.String()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("installation_id", "name", name="uq_tools_installation_name"),
        sa.UniqueConstraint("workspace_id", "llm_name", name="uq_tools_ws_llm_name"),
        sa.CheckConstraint("risk IN ('read','write','destructive')", name="ck_tools_risk"),
        sa.CheckConstraint(
            "capability_source IN ('declared','tagged','admin')",
            name="ck_tools_capability_source",
        ),
    )
    op.create_index("ix_tool_definitions_workspace_id", "tool_definitions", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_definitions_workspace_id", table_name="tool_definitions")
    op.drop_table("tool_definitions")
    op.drop_column("connector_installations", "last_synced_at")
