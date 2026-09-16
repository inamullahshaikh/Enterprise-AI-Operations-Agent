import uuid
from typing import Any

from sqlalchemy import select

from relay_core.db.models.documents import Document
from relay_core.db.repositories.base import WorkspaceScopedRepository


class DocumentRepository(WorkspaceScopedRepository[Document]):
    model = Document

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        collection_id: uuid.UUID,
        title: str,
        blob_key: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        uploaded_by: uuid.UUID,
    ) -> Document:
        document = Document(
            workspace_id=workspace_id,
            collection_id=collection_id,
            title=title,
            blob_key=blob_key,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            uploaded_by=uploaded_by,
        )
        self.session.add(document)
        await self.session.flush()
        return document

    async def get_by_sha256(
        self, workspace_id: uuid.UUID, collection_id: uuid.UUID, sha256: str
    ) -> Document | None:
        """Re-uploading byte-identical content re-uses the existing row rather than ingesting
        a duplicate (`documents`'s `(collection_id, sha256)` unique constraint)."""
        stmt = select(Document).where(
            Document.workspace_id == workspace_id,
            Document.collection_id == collection_id,
            Document.sha256 == sha256,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(
        self, workspace_id: uuid.UUID, ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, Document]:
        """Batch title lookup for retrieval results (`relay_core.rag.retrieval`), which knows
        chunks' `document_id`s but not their titles — one query per search instead of one
        per chunk."""
        if not ids:
            return {}
        stmt = select(Document).where(Document.workspace_id == workspace_id, Document.id.in_(ids))
        rows = (await self.session.execute(stmt)).scalars().all()
        return {d.id: d for d in rows}

    async def has_any_ready(self, workspace_id: uuid.UUID) -> bool:
        """Whether `knowledge.search` should be considered available for this workspace
        (`relay_core.capabilities.resolver`) — true once at least one document has finished
        ingesting, not merely once one has been uploaded (a `queued`/`processing`/`failed`
        document has nothing searchable yet)."""
        stmt = (
            select(Document.id)
            .where(Document.workspace_id == workspace_id, Document.status == "ready")
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def list_for_collection(
        self, workspace_id: uuid.UUID, collection_id: uuid.UUID
    ) -> list[Document]:
        stmt = (
            select(Document)
            .where(Document.workspace_id == workspace_id, Document.collection_id == collection_id)
            .order_by(Document.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def mark_processing(self, workspace_id: uuid.UUID, id_: uuid.UUID) -> Document:
        document = await self._require(workspace_id, id_)
        document.status = "processing"
        document.error = None
        return document

    async def mark_ready(
        self, workspace_id: uuid.UUID, id_: uuid.UUID, *, page_count: int | None, chunk_count: int
    ) -> Document:
        document = await self._require(workspace_id, id_)
        document.status = "ready"
        document.page_count = page_count
        document.chunk_count = chunk_count
        document.error = None
        return document

    async def mark_failed(self, workspace_id: uuid.UUID, id_: uuid.UUID, *, error: str) -> Document:
        document = await self._require(workspace_id, id_)
        document.status = "failed"
        document.error = error
        return document

    async def set_metadata(
        self, workspace_id: uuid.UUID, id_: uuid.UUID, metadata: dict[str, Any]
    ) -> Document:
        document = await self._require(workspace_id, id_)
        document.doc_metadata = metadata
        return document

    async def _require(self, workspace_id: uuid.UUID, id_: uuid.UUID) -> Document:
        document = await self.get(workspace_id, id_)
        if document is None:
            raise ValueError(f"documents row {id_} not found in workspace {workspace_id}")
        return document
