import uuid
from typing import Any

from sqlalchemy import select, tuple_

from relay_core.db.models.conversations import Message
from relay_core.db.repositories.base import WorkspaceScopedRepository


class MessageRepository(WorkspaceScopedRepository[Message]):
    model = Message

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        run_id: uuid.UUID | None = None,
        content_json: dict[str, Any] | None = None,
    ) -> Message:
        message = Message(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            run_id=run_id,
            content_json=content_json,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def list_for_conversation(
        self,
        workspace_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        limit: int = 50,
        before: uuid.UUID | None = None,
    ) -> list[Message]:
        """Most recent `limit` messages, oldest first — the shape a chat thread
        renders directly. `before` (a message id) pages further back in history.

        Ordered by `(created_at, id)`, not `created_at` alone: Postgres `now()`
        (this table's `created_at` server default) returns the *transaction's*
        start time, so messages inserted by the same transaction — e.g. a run's
        triggering user message and its assistant reply, when both happen to be
        written in one transaction, as an integration test's shared session
        does — can tie exactly. `id` breaks the tie correctly because it's a
        UUIDv7 generated in application code at each row's actual creation time,
        not transaction time.
        """
        stmt = select(Message).where(
            Message.workspace_id == workspace_id, Message.conversation_id == conversation_id
        )
        if before is not None:
            cursor = await self.session.get(Message, before)
            if cursor is not None:
                stmt = stmt.where(
                    tuple_(Message.created_at, Message.id) < (cursor.created_at, cursor.id)
                )
        stmt = stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
        rows = list((await self.session.execute(stmt)).scalars().all())
        rows.reverse()
        return rows
