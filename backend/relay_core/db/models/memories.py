"""Long-term memory (docs/system-design.md section 12 / 14.3), Phase 7's addition to the schema.

A row is one durable fact, preference or procedure extracted from a completed run. `user_id` is
null exactly when `scope = 'workspace'`: a workspace memory belongs to everybody in it, and a
user memory to one person in one workspace, never across workspaces.

Deletion is real deletion (section 12.2: users can view, edit and delete their memories).
`is_active` is for a memory somebody wants silenced without losing the record of it, and for
extraction to retire something it has superseded.

As in `tool_definitions`, there's no HNSW index on `embedding`: retrieval is always filtered to
one workspace and one user first, and an exact scan over that beats an approximate index that
filters after the fact.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, TimestampMixin, WorkspaceScoped, uuid7

SCOPES = ("user", "workspace")
KINDS = ("preference", "fact", "procedure")


class Memory(Base, WorkspaceScoped, TimestampMixin):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    scope: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_used_at: Mapped[datetime | None]
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("scope IN ('user','workspace')", name="ck_memories_scope"),
        CheckConstraint(
            "kind IN ('preference','fact','procedure')",
            name="ck_memories_kind",
        ),
        CheckConstraint("(scope = 'workspace') = (user_id IS NULL)", name="ck_memories_scope_user"),
        Index("ix_memories_scope", "workspace_id", "scope", "is_active"),
    )
