import uuid
from datetime import UTC, datetime

from sqlalchemy import desc, select

from relay_core.db.models.conversations import Conversation
from relay_core.db.repositories.base import WorkspaceScopedRepository


class ConversationRepository(WorkspaceScopedRepository[Conversation]):
    model = Conversation

    async def create(
        self, *, workspace_id: uuid.UUID, user_id: uuid.UUID, title: str | None = None
    ) -> Conversation:
        conversation = Conversation(workspace_id=workspace_id, user_id=user_id, title=title)
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def list_for_user(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.workspace_id == workspace_id, Conversation.user_id == user_id)
            .order_by(desc(Conversation.last_message_at), desc(Conversation.created_at))
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def touch(
        self, workspace_id: uuid.UUID, id_: uuid.UUID, *, title: str | None = None
    ) -> None:
        conversation = await self.get(workspace_id, id_)
        if conversation is None:
            return
        conversation.last_message_at = datetime.now(UTC)
        if title is not None and conversation.title is None:
            conversation.title = title
