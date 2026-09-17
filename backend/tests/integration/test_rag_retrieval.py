"""Coverage for the two retrieval legs (`relay_core.db.repositories.document_chunks`) and their
fusion/rerank (`relay_core.rag.retrieval.hybrid_search`) against a real Postgres + pgvector
(docs/system-design.md section 11.3). Embeddings here are small hand-crafted 768-dim vectors,
not real Gemini output — what's under test is cosine-distance ordering and RRF fusion, not
semantic quality, so a scripted embedding that's exactly right for the assertion is more useful
than a real one that isn't reproducible.
"""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from google.genai import types
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.documents import DocumentChunk
from relay_core.db.repositories.collections import CollectionRepository
from relay_core.db.repositories.document_chunks import DocumentChunkRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.llm_calls import LLMCallRepository, ModelPricingRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.ratelimit import RedisRateLimiter
from relay_core.rag.retrieval import hybrid_search

pytestmark = pytest.mark.asyncio

_DIM = 768


def _unit_vector(hot_index: int) -> list[float]:
    v = [0.0] * _DIM
    v[hot_index] = 1.0
    return v


def _graded_vector(primary_weight: float, secondary_weight: float) -> list[float]:
    """A vector on axes 0/1, normalized. Weighted toward axis 0 (the query vector) by
    `primary_weight` relative to `secondary_weight`, so distinct weight pairs produce a
    deterministic, non-tied cosine-distance ordering against `_unit_vector(0)` — unlike two
    `_unit_vector(i)`s for different i, which are equally (and thus non-deterministically
    ordered) orthogonal to the query."""
    import math

    norm = math.sqrt(primary_weight**2 + secondary_weight**2)
    v = [0.0] * _DIM
    v[0] = primary_weight / norm
    v[1] = secondary_weight / norm
    return v


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


async def _register_workspace(client: AsyncClient, email: str) -> tuple[uuid.UUID, uuid.UUID]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert resp.status_code == 201, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    user_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=headers)).json()["id"])
    ws_resp = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
    return uuid.UUID(ws_resp.json()["id"]), user_id


def _chunk(
    *,
    workspace_id: uuid.UUID,
    collection_id: uuid.UUID,
    document_id: uuid.UUID,
    ordinal: int,
    content: str,
    embedding: list[float],
    page_start: int | None = None,
) -> DocumentChunk:
    return DocumentChunk(
        workspace_id=workspace_id,
        collection_id=collection_id,
        document_id=document_id,
        ordinal=ordinal,
        content=content,
        context_header=f"Doc > section {ordinal}",
        page_start=page_start,
        token_count=len(content) // 4,
        content_hash=f"hash-{ordinal}",
        embedding=embedding,
        embedding_model="test-embed",
    )


async def _seeded_document(
    db_session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    collection = await CollectionRepository(db_session).get_or_create_default(workspace_id)
    document = await DocumentRepository(db_session).create(
        workspace_id=workspace_id,
        collection_id=collection.id,
        title="Renewal Playbook",
        blob_key="docs/renewal.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        sha256="abc123",
        uploaded_by=user_id,
    )
    await db_session.flush()
    return collection.id, document.id


async def test_vector_search_orders_by_cosine_distance(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, user_id = await _register_workspace(client, "rag-vector@example.com")
    collection_id, document_id = await _seeded_document(db_session, workspace_id, user_id)

    chunks = [
        _chunk(
            workspace_id=workspace_id,
            collection_id=collection_id,
            document_id=document_id,
            ordinal=0,
            content="closest",
            embedding=_unit_vector(0),
        ),
        _chunk(
            workspace_id=workspace_id,
            collection_id=collection_id,
            document_id=document_id,
            ordinal=1,
            content="farthest",
            embedding=_unit_vector(1),
        ),
    ]
    await DocumentChunkRepository(db_session).replace_for_document(
        workspace_id=workspace_id, document_id=document_id, chunks=chunks
    )

    results = await DocumentChunkRepository(db_session).vector_search(
        workspace_id=workspace_id,
        collection_ids=[collection_id],
        query_embedding=_unit_vector(0),
        limit=10,
    )
    assert [c.content for c, _score in results] == ["closest", "farthest"]
    assert results[0][1] == pytest.approx(1.0, abs=1e-6)


async def test_keyword_search_matches_full_text(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, user_id = await _register_workspace(client, "rag-keyword@example.com")
    collection_id, document_id = await _seeded_document(db_session, workspace_id, user_id)

    chunks = [
        _chunk(
            workspace_id=workspace_id,
            collection_id=collection_id,
            document_id=document_id,
            ordinal=0,
            content="Escalate to the account owner for renewal exceptions.",
            embedding=_unit_vector(0),
        ),
        _chunk(
            workspace_id=workspace_id,
            collection_id=collection_id,
            document_id=document_id,
            ordinal=1,
            content="Unrelated onboarding checklist content.",
            embedding=_unit_vector(1),
        ),
    ]
    await DocumentChunkRepository(db_session).replace_for_document(
        workspace_id=workspace_id, document_id=document_id, chunks=chunks
    )

    results = await DocumentChunkRepository(db_session).keyword_search(
        workspace_id=workspace_id,
        collection_ids=[collection_id],
        query_text="renewal exceptions",
        limit=10,
    )
    assert len(results) == 1
    assert "renewal exceptions" in results[0][0].content


async def test_replace_for_document_removes_stale_chunks(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, user_id = await _register_workspace(client, "rag-replace@example.com")
    collection_id, document_id = await _seeded_document(db_session, workspace_id, user_id)
    repo = DocumentChunkRepository(db_session)

    await repo.replace_for_document(
        workspace_id=workspace_id,
        document_id=document_id,
        chunks=[
            _chunk(
                workspace_id=workspace_id,
                collection_id=collection_id,
                document_id=document_id,
                ordinal=0,
                content="v1",
                embedding=_unit_vector(0),
            )
        ],
    )
    await repo.replace_for_document(
        workspace_id=workspace_id,
        document_id=document_id,
        chunks=[
            _chunk(
                workspace_id=workspace_id,
                collection_id=collection_id,
                document_id=document_id,
                ordinal=0,
                content="v2",
                embedding=_unit_vector(0),
            )
        ],
    )

    results = await repo.vector_search(
        workspace_id=workspace_id,
        collection_ids=[collection_id],
        query_embedding=_unit_vector(0),
        limit=10,
    )
    assert [c.content for c, _ in results] == ["v2"]


def _embed_response(vectors: list[list[float]]) -> types.EmbedContentResponse:
    return types.EmbedContentResponse(
        embeddings=[types.ContentEmbedding(values=v) for v in vectors]
    )


def _text_response(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=text)]),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=5,
            candidates_token_count=5,
            thoughts_token_count=0,
            cached_content_token_count=0,
        ),
    )


class _ScriptedModels:
    def __init__(self, embed_vectors: list[list[float]], rerank_text: str | None) -> None:
        self._embed_vectors = embed_vectors
        self._rerank_text = rerank_text

    async def embed_content(
        self, *, model: str, contents: Any, config: Any
    ) -> types.EmbedContentResponse:
        return _embed_response(self._embed_vectors)

    async def generate_content(
        self, *, model: str, contents: Any, config: Any
    ) -> types.GenerateContentResponse:
        assert self._rerank_text is not None, "no generate_content call was expected"
        return _text_response(self._rerank_text)


class _ScriptedClient:
    def __init__(self, embed_vectors: list[list[float]], rerank_text: str | None = None) -> None:
        self.aio = _Aio(_ScriptedModels(embed_vectors, rerank_text))


class _Aio:
    def __init__(self, models: _ScriptedModels) -> None:
        self.models = models


def _gateway(
    db_session: AsyncSession, redis_client: Redis, test_settings, client: Any
) -> LLMGateway:
    return LLMGateway(
        client,
        limiter=RedisRateLimiter(redis_client, rpm_limit=test_settings.gemini_rpm_limit),
        llm_calls=LLMCallRepository(db_session),
        pricing=ModelPricingRepository(db_session),
    )


async def test_hybrid_search_skips_rerank_when_the_fused_list_is_small(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id = await _register_workspace(client, "rag-hybrid-small@example.com")
    collection_id, document_id = await _seeded_document(db_session, workspace_id, user_id)
    await DocumentChunkRepository(db_session).replace_for_document(
        workspace_id=workspace_id,
        document_id=document_id,
        chunks=[
            _chunk(
                workspace_id=workspace_id,
                collection_id=collection_id,
                document_id=document_id,
                ordinal=0,
                content="Escalate renewals to the account owner.",
                embedding=_unit_vector(0),
                page_start=3,
            )
        ],
    )

    gateway = _gateway(db_session, redis_client, test_settings, _ScriptedClient([_unit_vector(0)]))
    results = await hybrid_search(
        chunk_repo=DocumentChunkRepository(db_session),
        document_repo=DocumentRepository(db_session),
        gateway=gateway,
        settings=test_settings,
        workspace_id=workspace_id,
        collection_ids=[collection_id],
        query="How do I escalate a renewal?",
        top_k=6,
    )
    assert len(results) == 1
    assert results[0].document_title == "Renewal Playbook"
    assert results[0].citation_label == "Renewal Playbook p.3"


async def test_hybrid_search_reranks_when_the_fused_list_exceeds_top_k(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id = await _register_workspace(client, "rag-hybrid-rerank@example.com")
    collection_id, document_id = await _seeded_document(db_session, workspace_id, user_id)
    # Deterministic, strictly-decreasing similarity to the query vector (_unit_vector(0)):
    # chunk 0 is the closest match pre-rerank, chunk 2 the furthest — RRF fusion alone would
    # rank them 0, 1, 2, so the assertion below only holds if the scripted rerank response
    # (which flips that to 2, 1, 0) actually took effect.
    weights = [(10.0, 1.0), (5.0, 1.0), (1.0, 1.0)]
    chunks = [
        _chunk(
            workspace_id=workspace_id,
            collection_id=collection_id,
            document_id=document_id,
            ordinal=i,
            content=f"chunk {i}",
            embedding=_graded_vector(*weights[i]),
        )
        for i in range(3)
    ]
    await DocumentChunkRepository(db_session).replace_for_document(
        workspace_id=workspace_id, document_id=document_id, chunks=chunks
    )

    # The reranker's structured response reverses relevance order: chunk 2 most relevant.
    rerank_json = (
        '{"scores": ['
        '{"index": 0, "relevance": 0.1}, '
        '{"index": 1, "relevance": 0.5}, '
        '{"index": 2, "relevance": 0.9}'
        "]}"
    )
    gateway = _gateway(
        db_session,
        redis_client,
        test_settings,
        _ScriptedClient([_unit_vector(0)], rerank_text=rerank_json),
    )
    results = await hybrid_search(
        chunk_repo=DocumentChunkRepository(db_session),
        document_repo=DocumentRepository(db_session),
        gateway=gateway,
        settings=test_settings,
        workspace_id=workspace_id,
        collection_ids=[collection_id],
        query="anything",
        top_k=2,
    )
    assert [r.chunk.content for r in results] == ["chunk 2", "chunk 1"]
