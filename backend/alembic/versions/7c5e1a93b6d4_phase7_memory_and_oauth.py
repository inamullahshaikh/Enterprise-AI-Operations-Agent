"""Phase 7: memories, connector_credentials.oauth_expires_at

Revision ID: 7c5e1a93b6d4
Revises: 4e8b2d71a9f3
Create Date: 2026-09-17

docs/system-design.md sections 12 (memory), 14.3 (`memories`, `connector_credentials`), 18.3
step 6 (OAuth refresh before expiry).

`oauth_expires_at` sits beside the encrypted blob rather than inside it because the refresh
sweep has to find installations that are about to expire without decrypting every credential in
the database. It is null for every connector that doesn't use OAuth, and for an OAuth
installation nobody has connected yet.

No vector index on `memories.embedding`: retrieval is filtered to one workspace and one user
before it ever sorts by distance, and an exact scan over that set is cheaper than an
approximate index that filters afterwards (the same reasoning as `tool_definitions`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c5e1a93b6d4"
down_revision: str | None = "4e8b2d71a9f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connector_credentials",
        sa.Column("oauth_expires_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
        ),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column(
            "source_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("scope IN ('user','workspace')", name="ck_memories_scope"),
        sa.CheckConstraint("kind IN ('preference','fact','procedure')", name="ck_memories_kind"),
        # A workspace memory belongs to everybody in the workspace, a user memory to exactly one
        # person: the two are the same statement, so the database enforces them as one.
        sa.CheckConstraint(
            "(scope = 'workspace') = (user_id IS NULL)", name="ck_memories_scope_user"
        ),
    )
    op.create_index("ix_memories_workspace_id", "memories", ["workspace_id"])
    op.create_index("ix_memories_scope", "memories", ["workspace_id", "scope", "is_active"])


def downgrade() -> None:
    op.drop_index("ix_memories_scope", table_name="memories")
    op.drop_index("ix_memories_workspace_id", table_name="memories")
    op.drop_table("memories")
    op.drop_column("connector_credentials", "oauth_expires_at")
