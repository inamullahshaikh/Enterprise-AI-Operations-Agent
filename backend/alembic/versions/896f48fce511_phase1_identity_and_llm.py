"""Phase 1: identity/tenancy tables and LLM usage/pricing tables

Revision ID: 896f48fce511
Revises:
Create Date: 2026-09-16

Covers the subset of docs/system-design.md section 14.3 needed for Phase 1
(docs/system-design.md section 28): users, workspaces, workspace_members,
refresh_tokens, model_pricing, llm_calls. `api_keys` and `workspace_policies`
are added later, with the features that use them.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "896f48fce511"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text()),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("google_sub", sa.String()),
        sa.Column("auth_provider", sa.String(), nullable=False, server_default="password"),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("auth_provider IN ('password','google')", name="ck_users_auth_provider"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("google_sub", name="uq_users_google_sub"),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("plan", sa.String(), nullable=False, server_default="free"),
        sa.Column("monthly_budget_usd", sa.Numeric(10, 2), nullable=False, server_default="10.00"),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("plan IN ('free','pro','enterprise')", name="ck_workspaces_plan"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )

    op.create_table(
        "workspace_members",
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('owner','admin','member','viewer')", name="ck_members_role"),
    )
    op.create_index("ix_members_user", "workspace_members", ["user_id"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("user_agent", sa.Text()),
        sa.Column("ip", postgresql.INET()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_user", "refresh_tokens", ["user_id"])

    op.create_table(
        "model_pricing",
        sa.Column("model", sa.String(), primary_key=True),
        sa.Column("input_per_mtok", sa.Numeric(10, 4), nullable=False),
        sa.Column("output_per_mtok", sa.Numeric(10, 4), nullable=False),
        sa.Column("cached_input_per_mtok", sa.Numeric(10, 4)),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("node", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("fallback_from", sa.String()),
        sa.Column("thinking_level", sa.String()),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("thought_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("finish_reason", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('ok','error','blocked')", name="ck_llm_calls_status"),
    )
    op.create_index("ix_llm_calls_ws_month", "llm_calls", ["workspace_id", "created_at"])

    # Placeholder pricing so the Phase 1 LLM gateway can compute a cost for every
    # configured model out of the box. These are illustrative, not current prices —
    # docs/system-design.md section 5.2: check the Gemini pricing page before relying
    # on them, and update this table (not code) when real prices are known.
    model_pricing = sa.table(
        "model_pricing",
        sa.column("model", sa.String()),
        sa.column("input_per_mtok", sa.Numeric(10, 4)),
        sa.column("output_per_mtok", sa.Numeric(10, 4)),
        sa.column("cached_input_per_mtok", sa.Numeric(10, 4)),
        sa.column("effective_from", sa.Date()),
    )
    op.bulk_insert(
        model_pricing,
        [
            {
                "model": "gemini-3.8-flash",
                "input_per_mtok": "0.30",
                "output_per_mtok": "2.50",
                "cached_input_per_mtok": "0.075",
                "effective_from": date(2026, 1, 1),
            },
            {
                "model": "gemini-3.7-flash",
                "input_per_mtok": "0.30",
                "output_per_mtok": "2.50",
                "cached_input_per_mtok": "0.075",
                "effective_from": date(2026, 1, 1),
            },
            {
                "model": "gemini-3.5-flash-lite",
                "input_per_mtok": "0.10",
                "output_per_mtok": "0.40",
                "cached_input_per_mtok": "0.025",
                "effective_from": date(2026, 1, 1),
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_calls_ws_month", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_table("model_pricing")
    op.drop_index("ix_refresh_user", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_members_user", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
    op.drop_table("users")
