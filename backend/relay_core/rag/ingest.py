"""Ingestion orchestration (docs/system-design.md section 11.1): parse -> (Gemini OCR fallback
for scanned PDFs) -> chunk -> embed -> store, run from the Celery `ingest` queue
(`relay_worker.tasks.ingest`) after a document upload.

Unchanged chunks are never re-embedded on re-ingest (section 11.2): `content_hash` is compared
against the document's existing chunks before calling `gateway.embed()`, and only chunks whose
hash isn't already present pay for a new embedding call.
"""

import uuid

from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings
from relay_core.db.models.documents import Document, DocumentChunk
from relay_core.db.repositories.document_chunks import DocumentChunkRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.profiles import PLANNER
from relay_core.rag.chunker import chunk_blocks
from relay_core.rag.parsers import ParsedDocument, parse_document
from relay_core.storage.object_store import ObjectStore

_OCR_SYSTEM_PROMPT = """Transcribe this document's visible text into clean Markdown.

Use '#'/'##'/etc. for section headings exactly as they appear in the document, and represent
any tables as Markdown tables. Transcribe only what is visibly present — no summaries, no
commentary, no content that isn't in the document."""


class IngestionError(Exception):
    pass


async def ingest_document(
    *,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    session: AsyncSession,
    object_store: ObjectStore,
    gateway: LLMGateway,
    settings: Settings,
) -> Document:
    documents = DocumentRepository(session)
    chunks_repo = DocumentChunkRepository(session)

    document = await documents.get(workspace_id, document_id)
    if document is None:
        raise IngestionError(f"documents row {document_id} not found in workspace {workspace_id}")

    await documents.mark_processing(workspace_id, document_id)
    await session.flush()

    try:
        raw = await object_store.get_bytes(document.blob_key)
        parsed = parse_document(document.mime_type, raw)
        if parsed.needs_ocr:
            parsed = await _ocr_with_gemini(
                gateway, settings, raw, document.mime_type, workspace_id=workspace_id
            )

        new_chunks = chunk_blocks(document.title, parsed.blocks)
        existing_by_hash = await chunks_repo.existing_content_hashes(workspace_id, document_id)

        to_embed = [c for c in new_chunks if c.content_hash not in existing_by_hash]
        vectors = (
            await gateway.embed(
                [
                    f"{c.context_header}\n\n{c.content}"
                    if settings.rag_embed_context_headers
                    else c.content
                    for c in to_embed
                ],
                task="RETRIEVAL_DOCUMENT",
                settings=settings,
            )
            if to_embed
            else []
        )
        vector_by_hash = {c.content_hash: v for c, v in zip(to_embed, vectors, strict=True)}

        rows = []
        for c in new_chunks:
            reused = existing_by_hash.get(c.content_hash)
            embedding = list(reused.embedding) if reused else vector_by_hash[c.content_hash]
            embedding_model = reused.embedding_model if reused else settings.embedding_model
            rows.append(
                DocumentChunk(
                    workspace_id=workspace_id,
                    collection_id=document.collection_id,
                    document_id=document_id,
                    ordinal=c.ordinal,
                    content=c.content,
                    context_header=c.context_header,
                    section_path=c.section_path,
                    page_start=c.page_start,
                    page_end=c.page_end,
                    token_count=c.token_count,
                    content_hash=c.content_hash,
                    embedding=embedding,
                    embedding_model=embedding_model,
                )
            )

        await chunks_repo.replace_for_document(
            workspace_id=workspace_id, document_id=document_id, chunks=rows
        )
        document = await documents.mark_ready(
            workspace_id, document_id, page_count=parsed.page_count, chunk_count=len(rows)
        )
    except Exception as exc:  # noqa: BLE001 - every failure must leave a readable status, not a crash
        await documents.mark_failed(workspace_id, document_id, error=str(exc))
        raise
    return document


async def _ocr_with_gemini(
    gateway: LLMGateway,
    settings: Settings,
    raw: bytes,
    mime_type: str,
    *,
    workspace_id: uuid.UUID,
) -> ParsedDocument:
    part = types.Part.from_bytes(data=raw, mime_type=mime_type)
    response = await gateway.generate(
        role=PLANNER,
        system=_OCR_SYSTEM_PROMPT,
        contents=[part],
        workspace_id=workspace_id,
        settings=settings,
    )
    return parse_document("text/markdown", (response.text or "").encode("utf-8"))
