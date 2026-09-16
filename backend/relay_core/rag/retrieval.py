"""Hybrid retrieval (docs/system-design.md section 11.3): a vector leg (pgvector cosine) and a
keyword leg (Postgres full-text) are fused with Reciprocal Rank Fusion, then Flash-Lite reranks
the fused candidates in one structured call — skipped when the fused list is already small
enough that reranking couldn't change which chunks make the cut.
"""

import uuid
from dataclasses import dataclass

from pydantic import BaseModel, Field

from relay_core.config import Settings
from relay_core.db.models.documents import DocumentChunk
from relay_core.db.repositories.document_chunks import DocumentChunkRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.profiles import LIGHT
from relay_core.llm.schemas import parse_structured

_LEG_LIMIT = 30
_RRF_K = 60
_FUSED_LIMIT = 20
_DEFAULT_TOP_K = 6

_RERANK_SYSTEM_PROMPT = """You score how relevant each candidate passage is to a search query,
for a retrieval-augmented answer. Score every candidate from 0.0 (irrelevant) to 1.0 (directly
answers the query). Judge relevance to the query only; ignore writing quality."""


@dataclass
class RetrievedChunk:
    chunk: DocumentChunk
    document_title: str
    score: float
    citation_label: str


class _RerankScore(BaseModel):
    index: int
    relevance: float = Field(ge=0.0, le=1.0)


class _RerankVerdict(BaseModel):
    scores: list[_RerankScore]


async def hybrid_search(
    *,
    chunk_repo: DocumentChunkRepository,
    document_repo: DocumentRepository,
    gateway: LLMGateway,
    settings: Settings,
    workspace_id: uuid.UUID,
    collection_ids: list[uuid.UUID],
    query: str,
    run_id: uuid.UUID | None = None,
    top_k: int = _DEFAULT_TOP_K,
) -> list[RetrievedChunk]:
    if not collection_ids or not query.strip():
        return []

    [query_embedding] = await gateway.embed([query], task="RETRIEVAL_QUERY", settings=settings)
    vector_hits = await chunk_repo.vector_search(
        workspace_id=workspace_id,
        collection_ids=collection_ids,
        query_embedding=query_embedding,
        limit=_LEG_LIMIT,
    )
    keyword_hits = await chunk_repo.keyword_search(
        workspace_id=workspace_id,
        collection_ids=collection_ids,
        query_text=query,
        limit=_LEG_LIMIT,
    )
    fused = _reciprocal_rank_fusion(vector_hits, keyword_hits)[:_FUSED_LIMIT]
    if not fused:
        return []

    if len(fused) <= top_k:
        ranked = fused
    else:
        ranked = await _rerank(
            gateway, settings, query, fused, workspace_id=workspace_id, run_id=run_id
        )
    top = ranked[:top_k]

    titles = await document_repo.get_many(workspace_id, [chunk.document_id for chunk, _ in top])
    results: list[RetrievedChunk] = []
    for chunk, score in top:
        title = titles[chunk.document_id].title if chunk.document_id in titles else "Unknown"
        citation_label = f"{title} p.{chunk.page_start}" if chunk.page_start else title
        results.append(
            RetrievedChunk(
                chunk=chunk, document_title=title, score=score, citation_label=citation_label
            )
        )
    return results


def _reciprocal_rank_fusion(
    *legs: list[tuple[DocumentChunk, float]], k: int = _RRF_K
) -> list[tuple[DocumentChunk, float]]:
    rrf_scores: dict[uuid.UUID, float] = {}
    chunks_by_id: dict[uuid.UUID, DocumentChunk] = {}
    for leg in legs:
        for rank, (chunk, _leg_score) in enumerate(leg, start=1):
            rrf_scores[chunk.id] = rrf_scores.get(chunk.id, 0.0) + 1.0 / (k + rank)
            chunks_by_id[chunk.id] = chunk
    ordered_ids = sorted(rrf_scores, key=lambda cid: rrf_scores[cid], reverse=True)
    return [(chunks_by_id[cid], rrf_scores[cid]) for cid in ordered_ids]


async def _rerank(
    gateway: LLMGateway,
    settings: Settings,
    query: str,
    candidates: list[tuple[DocumentChunk, float]],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID | None,
) -> list[tuple[DocumentChunk, float]]:
    listing = "\n\n".join(
        f"[{i}] {chunk.context_header}\n{chunk.content[:500]}"
        for i, (chunk, _score) in enumerate(candidates)
    )
    response = await gateway.generate(
        role=LIGHT,
        system=_RERANK_SYSTEM_PROMPT,
        contents=f"Query: {query}\n\nCandidates:\n{listing}",
        workspace_id=workspace_id,
        run_id=run_id,
        response_schema=_RerankVerdict,
        settings=settings,
    )
    verdict = parse_structured(response, _RerankVerdict)
    if verdict is None or not verdict.scores:
        return candidates  # fall back to RRF order rather than fail retrieval outright

    relevance_by_index = {s.index: s.relevance for s in verdict.scores}
    scored = [
        (chunk, relevance_by_index.get(i, 0.0)) for i, (chunk, _rrf) in enumerate(candidates)
    ]
    return sorted(scored, key=lambda pair: pair[1], reverse=True)
