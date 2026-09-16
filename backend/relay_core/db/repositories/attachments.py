import uuid
from typing import Any

from sqlalchemy import select

from relay_core.db.models.attachments import Attachment
from relay_core.db.repositories.base import WorkspaceScopedRepository


class AttachmentRepository(WorkspaceScopedRepository[Attachment]):
    model = Attachment

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        conversation_id: uuid.UUID,
        filename: str,
        mime_type: str,
        size_bytes: int,
        blob_key: str,
        kind: str,
        profile: dict[str, Any] | None,
        inferred_capabilities: list[str],
        uploaded_by: uuid.UUID,
    ) -> Attachment:
        attachment = Attachment(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            blob_key=blob_key,
            kind=kind,
            profile=profile,
            inferred_capabilities=inferred_capabilities,
            uploaded_by=uploaded_by,
        )
        self.session.add(attachment)
        await self.session.flush()
        return attachment

    async def list_for_conversation(
        self, workspace_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[Attachment]:
        stmt = (
            select(Attachment)
            .where(
                Attachment.workspace_id == workspace_id,
                Attachment.conversation_id == conversation_id,
            )
            .order_by(Attachment.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())
