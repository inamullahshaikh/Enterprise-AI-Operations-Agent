"""Tool definitions (docs/system-design.md section 14.3), Phase 6: one row per tool of every
*installed* connector — built-ins, MCP and OpenAPI alike — so discovery, admin review, risk and
capability overrides, and retrieval embeddings all work the same way whatever a tool's source.
The always-available connectors (`file_upload`, `documents`, `python_sandbox`) have no
installation row and so no tool rows; they still bind live from code.

Trimmed from the design doc: no `output_schema` (nothing reads it), no GIN index on
`capabilities` and no HNSW index on `embedding`. A workspace has tens of tools, and an exact
scan over one workspace's rows beats an approximate index that filters after the fact.
"""

import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import REAL, Boolean, CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, TimestampMixin, WorkspaceScoped, uuid7


class ToolDefinition(Base, WorkspaceScoped, TimestampMixin):
    __tablename__ = "tool_definitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connector_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    llm_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # sha256 of {description, input_schema}: an upstream change to either is what flags an MCP
    # tool for review (section 6.4's rug-pull defence).
    schema_hash: Mapped[str] = mapped_column(String, nullable=False)
    risk: Mapped[str] = mapped_column(String, nullable=False)
    risk_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )
    capability_source: Mapped[str] = mapped_column(
        String, nullable=False, default="declared", server_default="declared"
    )
    tag_confidence: Mapped[float | None] = mapped_column(REAL)
    idempotent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    timeout_s: Mapped[float] = mapped_column(REAL, nullable=False, default=30, server_default="30")
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768))
    embedding_model: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("installation_id", "name", name="uq_tools_installation_name"),
        UniqueConstraint("workspace_id", "llm_name", name="uq_tools_ws_llm_name"),
        CheckConstraint("risk IN ('read','write','destructive')", name="ck_tools_risk"),
        CheckConstraint(
            "capability_source IN ('declared','tagged','admin')", name="ck_tools_capability_source"
        ),
    )
