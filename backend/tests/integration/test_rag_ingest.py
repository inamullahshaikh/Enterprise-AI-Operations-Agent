"""Coverage for `relay_core.rag.ingest.ingest_document` (docs/system-design.md section 11.1):
the parse -> chunk -> embed -> store pipeline, the scanned-PDF Gemini-OCR fallback branch, the
content-hash skip-re-embedding optimization on re-ingest, and that a mid-pipeline failure leaves
the document readably `failed` rather than stuck `processing` or silently swallowed.
"""

import uuid
from typing import Any

import pymupdf
import pytest
import pytest_asyncio
from google.genai import types
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.repositories.collections import CollectionRepository
from relay_core.db.repositories.document_chunks import DocumentChunkRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.llm_calls import LLMCallRepository, ModelPricingRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.ratelimit import RedisRateLimiter
from relay_core.rag.ingest import IngestionError, ingest_document

pytestmark = pytest.mark.asyncio


class _FakeObjectStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        self.blobs[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.blobs[key]


class _RaisingObjectStore:
    async def get_bytes(self, key: str) -> bytes:
        raise RuntimeError("R2 is unreachable")


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


async def _make_document(
    db_session: AsyncSession,
    object_store: _FakeObjectStore,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str,
    mime_type: str,
    raw: bytes,
) -> uuid.UUID:
    collection = await CollectionRepository(db_session).get_or_create_default(workspace_id)
    blob_key = f"documents/{workspace_id}/{uuid.uuid4()}"
    object_store.blobs[blob_key] = raw
    document = await DocumentRepository(db_session).create(
        workspace_id=workspace_id,
        collection_id=collection.id,
        title=title,
        blob_key=blob_key,
        mime_type=mime_type,
        size_bytes=len(raw),
        sha256="deadbeef",
        uploaded_by=user_id,
    )
    await db_session.flush()
    return document.id


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


def _embed_response(n: int) -> types.EmbedContentResponse:
    return types.EmbedContentResponse(
        embeddings=[types.ContentEmbedding(values=[0.1] * 768) for _ in range(n)]
    )


class _ScriptedModels:
    def __init__(self, *, generate_text: str = "", fail_on_embed: bool = False) -> None:
        self._generate_text = generate_text
        self._fail_on_embed = fail_on_embed
        self.embed_calls: list[list[str]] = []

    async def embed_content(
        self, *, model: str, contents: Any, config: Any
    ) -> types.EmbedContentResponse:
        if self._fail_on_embed:
            raise AssertionError("embed_content should not have been called")
        texts = list(contents)
        self.embed_calls.append(texts)
        return _embed_response(len(texts))

    async def generate_content(
        self, *, model: str, contents: Any, config: Any
    ) -> types.GenerateContentResponse:
        return _text_response(self._generate_text)


class _ScriptedClient:
    def __init__(self, models: _ScriptedModels) -> None:
        self.aio = _Aio(models)


class _Aio:
    def __init__(self, models: _ScriptedModels) -> None:
        self.models = models


def _gateway(
    db_session: AsyncSession, redis_client: Redis, test_settings, models: _ScriptedModels
) -> LLMGateway:
    return LLMGateway(
        _ScriptedClient(models),
        limiter=RedisRateLimiter(redis_client, rpm_limit=test_settings.gemini_rpm_limit),
        llm_calls=LLMCallRepository(db_session),
        pricing=ModelPricingRepository(db_session),
    )


async def test_ingesting_a_text_document_produces_ready_chunks(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id = await _register_workspace(client, "ingest-text@example.com")
    object_store = _FakeObjectStore()
    document_id = await _make_document(
        db_session,
        object_store,
        workspace_id=workspace_id,
        user_id=user_id,
        title="Notes",
        mime_type="text/plain",
        raw=b"First paragraph.\n\nSecond paragraph.",
    )
    models = _ScriptedModels()
    gateway = _gateway(db_session, redis_client, test_settings, models)

    document = await ingest_document(
        workspace_id=workspace_id,
        document_id=document_id,
        session=db_session,
        object_store=object_store,
        gateway=gateway,
        settings=test_settings,
    )

    assert document.status == "ready"
    # Both paragraphs are far under the 500-token target, so the chunker packs them into one
    # chunk rather than one-chunk-per-paragraph (docs/system-design.md section 11.2).
    assert document.chunk_count == 1
    assert len(models.embed_calls) == 1
    assert models.embed_calls[0] == ["First paragraph.\n\nSecond paragraph."]

    stored = await DocumentChunkRepository(db_session).existing_content_hashes(
        workspace_id, document_id
    )
    assert len(stored) == 1


async def test_reingesting_unchanged_content_never_calls_embed_again(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id = await _register_workspace(client, "ingest-reingest@example.com")
    object_store = _FakeObjectStore()
    document_id = await _make_document(
        db_session,
        object_store,
        workspace_id=workspace_id,
        user_id=user_id,
        title="Notes",
        mime_type="text/plain",
        raw=b"Stable content that never changes.",
    )
    models = _ScriptedModels()
    gateway = _gateway(db_session, redis_client, test_settings, models)

    first = await ingest_document(
        workspace_id=workspace_id,
        document_id=document_id,
        session=db_session,
        object_store=object_store,
        gateway=gateway,
        settings=test_settings,
    )
    assert len(models.embed_calls) == 1

    second = await ingest_document(
        workspace_id=workspace_id,
        document_id=document_id,
        session=db_session,
        object_store=object_store,
        gateway=gateway,
        settings=test_settings,
    )
    # No new embed_content call: every chunk's content_hash already existed.
    assert len(models.embed_calls) == 1
    assert second.chunk_count == first.chunk_count


async def test_a_scanned_pdf_falls_back_to_gemini_transcription(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id = await _register_workspace(client, "ingest-scanned@example.com")
    object_store = _FakeObjectStore()
    blank_pdf = pymupdf.open()
    blank_pdf.new_page()
    document_id = await _make_document(
        db_session,
        object_store,
        workspace_id=workspace_id,
        user_id=user_id,
        title="Scanned Policy",
        mime_type="application/pdf",
        raw=blank_pdf.tobytes(),
    )
    models = _ScriptedModels(generate_text="# Scanned Policy\n\nTranscribed body text.\n")
    gateway = _gateway(db_session, redis_client, test_settings, models)

    document = await ingest_document(
        workspace_id=workspace_id,
        document_id=document_id,
        session=db_session,
        object_store=object_store,
        gateway=gateway,
        settings=test_settings,
    )

    assert document.status == "ready"
    assert document.chunk_count == 1
    stored = await DocumentChunkRepository(db_session).existing_content_hashes(
        workspace_id, document_id
    )
    (chunk,) = stored.values()
    assert "Transcribed body text." in chunk.content


async def test_a_failure_marks_the_document_failed_and_still_raises(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id = await _register_workspace(client, "ingest-fail@example.com")
    object_store = _FakeObjectStore()
    document_id = await _make_document(
        db_session,
        object_store,
        workspace_id=workspace_id,
        user_id=user_id,
        title="Doomed",
        mime_type="text/plain",
        raw=b"irrelevant",
    )
    models = _ScriptedModels(fail_on_embed=True)
    gateway = _gateway(db_session, redis_client, test_settings, models)

    with pytest.raises(RuntimeError, match="R2 is unreachable"):
        await ingest_document(
            workspace_id=workspace_id,
            document_id=document_id,
            session=db_session,
            object_store=_RaisingObjectStore(),
            gateway=gateway,
            settings=test_settings,
        )

    document = await DocumentRepository(db_session).get(workspace_id, document_id)
    assert document is not None
    assert document.status == "failed"
    assert document.error is not None and "R2 is unreachable" in document.error


async def test_ingest_document_raises_for_an_unknown_document(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, _user_id = await _register_workspace(client, "ingest-missing@example.com")
    gateway = _gateway(db_session, redis_client, test_settings, _ScriptedModels())

    with pytest.raises(IngestionError):
        await ingest_document(
            workspace_id=workspace_id,
            document_id=uuid.uuid4(),
            session=db_session,
            object_store=_FakeObjectStore(),
            gateway=gateway,
            settings=test_settings,
        )
