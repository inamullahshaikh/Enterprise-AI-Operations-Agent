"""Conversation attachments (docs/system-design.md section 14.3), trimmed to the CSV-only
subset `file_upload` needs this phase (docs/adr/0009): no `message_id`/`status` — profiling
happens synchronously on upload (section 11.5), not through the async ingestion pipeline
that PDFs/DOCX need starting Phase 4, so there's no "still processing" state to represent yet.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, WorkspaceScoped, uuid7


class Attachment(Base, WorkspaceScoped):
    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    blob_key: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    inferred_capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint("kind IN ('table','document','image','other')", name="ck_attachments_kind"),
    )
