"""Conversations & messages (docs/system-design.md section 14.3).

`run_steps`, `tool_calls`, and `approvals` are Phase 3/5 tables that don't exist
yet, so `messages.run_id` only ever points at a run that produced an assistant
message via `finalize`/`ask_missing` — the triggering user message's `run_id`
stays null (the link the other way is `agent_runs.trigger_message_id`).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, TimestampMixin, WorkspaceScoped, uuid7


class Conversation(Base, WorkspaceScoped, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    # Rolling summary of older messages (docs/system-design.md section 12.1) — a
    # Phase 7 feature; the column exists now so `load_context` has a stable place
    # to read `None` from rather than a later migration adding it under load.
    summary: Mapped[str | None] = mapped_column(Text)
    summary_upto_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_archived: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")
    last_message_at: Mapped[datetime | None]


class Message(Base, WorkspaceScoped):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # No FK in the ORM declaration: `agent_runs` is declared after this module and
    # the FK itself is only added once both tables exist (see the Phase 2
    # migration's trailing ALTER), matching the forward-reference note already on
    # `relay_core.db.models.llm.LLMCall.run_id`.
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    token_count: Mapped[int | None]
    feedback: Mapped[int | None] = mapped_column(SmallInteger)
    feedback_comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system')", name="ck_messages_role"),
        CheckConstraint("feedback IN (-1, 1)", name="ck_messages_feedback"),
    )
