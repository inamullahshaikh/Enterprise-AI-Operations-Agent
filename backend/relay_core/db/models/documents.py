"""Knowledge base tables for RAG (docs/system-design.md section 11 / 14.3), Phase 4's
addition to the schema. `documents.source_type` keeps the full `upload|url|connector_sync`
vocabulary from the design doc even though Phase 4 only ever writes `'upload'` (FR-16: users
upload files to a conversation or the workspace knowledge base) — `url`/`connector_sync` are
for a later phase's sync jobs and don't need a migration of their own once they exist.

`document_chunks.tsv` is a generated column (`GENERATED ALWAYS AS ... STORED`), computed by
Postgres itself from `context_header`/`content` rather than written by the ingestion pipeline,
so it can never drift out of sync with the text it indexes.
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, TimestampMixin, WorkspaceScoped, uuid7

_ALL_ROLES = ["owner", "admin", "member", "viewer"]


class Collection(Base, WorkspaceScoped):
    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    visible_roles: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=lambda: list(_ALL_ROLES)
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_collections_ws_name"),)


class Document(Base, WorkspaceScoped, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False, default="upload")
    source_uri: Mapped[str | None] = mapped_column(Text)
    blob_key: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    error: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("collection_id", "sha256", name="uq_documents_collection_sha256"),
        CheckConstraint(
            "source_type IN ('upload','url','connector_sync')", name="ck_documents_source_type"
        ),
        CheckConstraint(
            "status IN ('queued','processing','ready','failed')", name="ck_documents_status"
        ),
    )


class DocumentChunk(Base, WorkspaceScoped):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    context_header: Mapped[str] = mapped_column(Text, nullable=False)
    section_path: Mapped[str | None] = mapped_column(Text)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    # Populated by Postgres itself (see module docstring), never written by the ORM.
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', context_header || ' ' || content)", persisted=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    __table_args__ = (
        Index("ix_chunks_scope", "workspace_id", "collection_id"),
        Index("ix_chunks_document", "document_id"),
    )
