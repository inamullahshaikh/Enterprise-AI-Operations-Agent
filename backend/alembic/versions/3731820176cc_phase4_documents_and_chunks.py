"""Phase 4: pgvector extension, collections, documents, document_chunks

Revision ID: 3731820176cc
Revises: 15f2ea199473
Create Date: 2026-09-16

docs/system-design.md section 11 (RAG & document ingestion) / section 14.3. The `vector`
extension was declared in section 14.3's DDL from Phase 1 but never actually enabled — nothing
needed it until this migration. `document_chunks.tsv` is a `GENERATED ALWAYS ... STORED` column
so Postgres itself keeps the full-text index in sync with `context_header`/`content`; the
ingestion pipeline never writes to it directly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3731820176cc"
down_revision: str | None = "15f2ea199473"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALL_ROLES = "{owner,admin,member,viewer}"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "collections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "visible_roles",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=_ALL_ROLES,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("workspace_id", "name", name="uq_collections_ws_name"),
    )
    op.create_index("ix_collections_ws", "collections", ["workspace_id"])

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "collection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False, server_default="upload"),
        sa.Column("source_uri", sa.Text()),
        sa.Column("blob_key", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("page_count", sa.Integer()),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("error", sa.Text()),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("collection_id", "sha256", name="uq_documents_collection_sha256"),
        sa.CheckConstraint(
            "source_type IN ('upload','url','connector_sync')", name="ck_documents_source_type"
        ),
        sa.CheckConstraint(
            "status IN ('queued','processing','ready','failed')", name="ck_documents_status"
        ),
    )
    op.create_index("ix_documents_ws_collection", "documents", ["workspace_id", "collection_id"])

    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("context_header", sa.Text(), nullable=False),
        sa.Column("section_path", sa.Text()),
        sa.Column("page_start", sa.Integer()),
        sa.Column("page_end", sa.Integer()),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', context_header || ' ' || content)", persisted=True
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_chunks_scope", "document_chunks", ["workspace_id", "collection_id"])
    op.create_index("ix_chunks_document", "document_chunks", ["document_id"])
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute("CREATE INDEX ix_chunks_tsv ON document_chunks USING gin(tsv)")


def downgrade() -> None:
    op.drop_index("ix_chunks_tsv", table_name="document_chunks")
    op.drop_index("ix_chunks_embedding", table_name="document_chunks")
    op.drop_index("ix_chunks_document", table_name="document_chunks")
    op.drop_index("ix_chunks_scope", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_ws_collection", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_collections_ws", table_name="collections")
    op.drop_table("collections")
