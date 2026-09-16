"""Identity & tenancy tables (docs/system-design.md section 14.3).

Only the Phase 1 subset is modelled here: `api_keys` and `workspace_policies` belong
to later phases (governance / API-key auth) and are added with the migrations that
introduce the features that use them.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import CITEXT, INET, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from relay_core.db.base import Base, TimestampMixin, uuid7


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Google Sign-In (docs/adr/0007-google-sign-in.md). `google_sub` is the stable
    # Google account id ("subject" claim); nullable because password-only accounts
    # never set it. `auth_provider` records how the account was first created;
    # an account can still hold both a password and a linked Google identity.
    google_sub: Mapped[str | None] = mapped_column(String, unique=True)
    auth_provider: Mapped[str] = mapped_column(
        String, nullable=False, default="password", server_default="password"
    )

    last_login_at: Mapped[datetime | None]

    memberships: Mapped[list["WorkspaceMember"]] = relationship(
        back_populates="user", foreign_keys="WorkspaceMember.user_id"
    )

    __table_args__ = (
        CheckConstraint("auth_provider IN ('password','google')", name="ck_users_auth_provider"),
    )


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String, nullable=False, default="free", server_default="free")
    monthly_budget_usd: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, default=10.00, server_default="10.00"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    members: Mapped[list["WorkspaceMember"]] = relationship(back_populates="workspace")

    __table_args__ = (
        CheckConstraint("plan IN ('free','pro','enterprise')", name="ck_workspaces_plan"),
    )


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    joined_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    workspace: Mapped["Workspace"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships", foreign_keys=[user_id])

    __table_args__ = (
        CheckConstraint("role IN ('owner','admin','member','viewer')", name="ck_members_role"),
    )


class RefreshToken(Base):
    """Opaque, rotated refresh tokens (docs/system-design.md section 18.2).

    Only the sha256 hash of the token is stored. `family_id` links every token
    descended from one login; if a revoked or already-rotated token is presented
    again, the whole family is revoked (reuse detection — a stolen token being used
    after the legitimate client already rotated it).
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None]
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")
