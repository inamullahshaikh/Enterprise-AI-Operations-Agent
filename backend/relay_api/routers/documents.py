"""Knowledge-base document upload routes (docs/system-design.md section 11.1, FR-16: "Users can
upload files ... to a conversation or to the workspace knowledge base" — this is the *knowledge
base* half; conversation attachments are `relay_api/routers/conversations.py`'s CSV-only
endpoint). Every workspace ingests into one implicit default collection until there's an admin
UI for creating more (`relay_core.db.repositories.collections.CollectionRepository`'s
docstring).

Upload only writes the `documents` row and enqueues ingestion — parsing/chunking/embedding
happens on the `ingest` queue (`relay_worker.tasks.ingest`), the same async-processing split
`conversations.py`'s attachment endpoint's docstring calls out as the reason CSV stayed
synchronous in Phase 3 and everything else waited for this phase.
"""

import hashlib
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Path, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import (
    CurrentUser,
    IngestDispatcher,
    get_ingest_dispatcher,
    get_object_store,
    require_workspace_role,
)
from relay_core.db.models.documents import Document
from relay_core.db.repositories.collections import CollectionRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.session import get_session
from relay_core.security.rbac import Role
from relay_core.storage.object_store import ObjectStore

router = APIRouter(prefix="/workspaces/{workspace_id}/documents", tags=["documents"])

_MIME_TYPES_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    mime_type: str
    size_bytes: int
    status: str
    error: str | None
    page_count: int | None
    chunk_count: int

    @classmethod
    def from_model(cls, d: Document) -> "DocumentOut":
        return cls(
            id=d.id,
            title=d.title,
            mime_type=d.mime_type,
            size_bytes=d.size_bytes,
            status=d.status,
            error=d.error,
            page_count=d.page_count,
            chunk_count=d.chunk_count,
        )


def _mime_type_for(filename: str) -> str:
    lower = filename.lower()
    for suffix, mime_type in _MIME_TYPES_BY_EXTENSION.items():
        if lower.endswith(suffix):
            return mime_type
    supported = ", ".join(sorted(_MIME_TYPES_BY_EXTENSION))
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST, f"Unsupported file type. Supported: {supported}"
    )


@router.post("", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    workspace_id: uuid.UUID = Path(...),
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_workspace_role(Role.member.name)),
    session: AsyncSession = Depends(get_session),
    object_store: ObjectStore = Depends(get_object_store),
    dispatch: IngestDispatcher = Depends(get_ingest_dispatcher),
) -> DocumentOut:
    filename = file.filename or "upload"
    mime_type = _mime_type_for(filename)
    raw = await file.read()
    sha256 = hashlib.sha256(raw).hexdigest()

    collections = CollectionRepository(session)
    documents = DocumentRepository(session)
    collection = await collections.get_or_create_default(workspace_id)

    existing = await documents.get_by_sha256(workspace_id, collection.id, sha256)
    if existing is not None:
        # Byte-identical re-upload: return the existing row rather than re-ingesting
        # (DocumentRepository.get_by_sha256's docstring).
        return DocumentOut.from_model(existing)

    blob_key = f"documents/{workspace_id}/{uuid.uuid4()}/{filename}"
    await object_store.put_bytes(blob_key, raw, content_type=mime_type)

    document = await documents.create(
        workspace_id=workspace_id,
        collection_id=collection.id,
        title=filename,
        blob_key=blob_key,
        mime_type=mime_type,
        size_bytes=len(raw),
        sha256=sha256,
        uploaded_by=current.user.id,
    )
    await session.flush()
    await dispatch(workspace_id, document.id)

    return DocumentOut.from_model(document)


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> list[DocumentOut]:
    collection = await CollectionRepository(session).get_or_create_default(workspace_id)
    documents = await DocumentRepository(session).list_for_collection(workspace_id, collection.id)
    return [DocumentOut.from_model(d) for d in documents]


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    workspace_id: uuid.UUID = Path(...),
    document_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    document = await DocumentRepository(session).get(workspace_id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return DocumentOut.from_model(document)
