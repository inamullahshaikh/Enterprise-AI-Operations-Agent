"""Chunk storage and the two retrieval legs hybrid search fuses (docs/system-design.md section
11.3): pgvector cosine distance for the vector leg, Postgres full-text search
(`websearch_to_tsquery` against the generated `tsv` column) for the keyword leg. RRF fusion and
reranking are the caller's job (`relay_core.rag.retrieval`) — this repository only knows how to
run each leg and return `(chunk, score)` pairs.
"""

import uuid

from sqlalchemy import delete, func, select

from relay_core.db.models.documents import DocumentChunk
from relay_core.db.repositories.base import WorkspaceScopedRepository


class DocumentChunkRepository(WorkspaceScopedRepository[DocumentChunk]):
    model = DocumentChunk

    async def replace_for_document(
        self,
        *,
        workspace_id: uuid.UUID,
        document_id: uuid.UUID,
        chunks: list[DocumentChunk],
    ) -> None:
        """Re-ingesting a document (e.g. an edited re-upload that changed its sha256, or a
        forced re-chunk) replaces its chunks wholesale rather than diffing — `content_hash` is
        used one level up, by the ingestion pipeline, to skip re-embedding chunks whose text
        didn't change (docs/system-design.md section 11.2), not to avoid this delete."""
        await self.session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.workspace_id == workspace_id,
                DocumentChunk.document_id == document_id,
            )
        )
        self.session.add_all(chunks)
        await self.session.flush()

    async def existing_content_hashes(
        self, workspace_id: uuid.UUID, document_id: uuid.UUID
    ) -> dict[str, DocumentChunk]:
        rows = await self.list_for_document(workspace_id, document_id)
        return {chunk.content_hash: chunk for chunk in rows}

    async def list_for_document(
        self, workspace_id: uuid.UUID, document_id: uuid.UUID
    ) -> list[DocumentChunk]:
        """A document's chunks in reading order — used by the `documents` connector's
        `get_document` tool (docs/system-design.md section 10.2), and by
        `existing_content_hashes` above for the ingestion pipeline's skip-re-embedding check."""
        stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.workspace_id == workspace_id, DocumentChunk.document_id == document_id
            )
            .order_by(DocumentChunk.ordinal)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def vector_search(
        self,
        *,
        workspace_id: uuid.UUID,
        collection_ids: list[uuid.UUID],
        query_embedding: list[float],
        limit: int = 30,
    ) -> list[tuple[DocumentChunk, float]]:
        if not collection_ids:
            return []
        distance = DocumentChunk.embedding.cosine_distance(query_embedding)
        stmt = (
            select(DocumentChunk, (1 - distance).label("score"))
            .where(
                DocumentChunk.workspace_id == workspace_id,
                DocumentChunk.collection_id.in_(collection_ids),
            )
            .order_by(distance)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(chunk, float(score)) for chunk, score in rows]

    async def keyword_search(
        self,
        *,
        workspace_id: uuid.UUID,
        collection_ids: list[uuid.UUID],
        query_text: str,
        limit: int = 30,
    ) -> list[tuple[DocumentChunk, float]]:
        if not collection_ids:
            return []
        tsquery = func.websearch_to_tsquery("english", query_text)
        rank = func.ts_rank_cd(DocumentChunk.tsv, tsquery)
        stmt = (
            select(DocumentChunk, rank.label("score"))
            .where(
                DocumentChunk.workspace_id == workspace_id,
                DocumentChunk.collection_id.in_(collection_ids),
                DocumentChunk.tsv.op("@@")(tsquery),
            )
            .order_by(rank.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(chunk, float(score)) for chunk, score in rows]
