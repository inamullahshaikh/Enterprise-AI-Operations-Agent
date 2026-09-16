"""End-to-end check of the Phase 1 "done when" (docs/system-design.md section
28): a test endpoint calls Gemini with a Pydantic schema and the call appears in
`llm_calls`. The Gemini client itself is faked (no real API key / network call),
but everything around it — auth, workspace RBAC, the real LLM gateway, a real
Redis-backed rate limiter, and a real `llm_calls` insert — is exercised for real.
"""

import uuid

import pytest
from google.genai import types
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_genai_client
from relay_api.main import app
from relay_core.db.models.llm import LLMCall

pytestmark = pytest.mark.asyncio


class _FakeModels:
    async def generate_content(self, *, model, contents, config):
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text='{"greeting":"Bonjour","language":"fr"}')],
                    ),
                    finish_reason=types.FinishReason.STOP,
                )
            ],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=12,
                candidates_token_count=6,
                thoughts_token_count=0,
                cached_content_token_count=0,
            ),
        )


class _FakeAio:
    models = _FakeModels()


class _FakeGenaiClient:
    aio = _FakeAio()


async def test_gemini_ping_records_usage_in_llm_calls(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    app.dependency_overrides[get_genai_client] = lambda: _FakeGenaiClient()
    try:
        register_resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "dev@example.com",
                "password": "correct horse battery staple",
                "full_name": "Dev",
            },
        )
        assert register_resp.status_code == 201
        token = register_resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        ws_resp = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
        assert ws_resp.status_code == 201
        workspace_id = uuid.UUID(ws_resp.json()["id"])

        ping_resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/_debug/gemini-ping", headers=headers
        )
        assert ping_resp.status_code == 200, ping_resp.text
        body = ping_resp.json()
        assert body["text"] == '{"greeting":"Bonjour","language":"fr"}'
        assert body["model"]
        assert float(body["cost_usd"]) >= 0

        rows = (
            (await db_session.execute(select(LLMCall).where(LLMCall.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "ok"
        assert rows[0].input_tokens == 12
        assert rows[0].output_tokens == 6
    finally:
        del app.dependency_overrides[get_genai_client]


async def test_gemini_ping_requires_workspace_membership(client: AsyncClient) -> None:
    app.dependency_overrides[get_genai_client] = lambda: _FakeGenaiClient()
    try:
        register_resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "outsider2@example.com",
                "password": "correct horse battery staple",
                "full_name": "O",
            },
        )
        token = register_resp.json()["access_token"]
        resp = await client.post(
            f"/api/v1/workspaces/{uuid.uuid4()}/_debug/gemini-ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404
    finally:
        del app.dependency_overrides[get_genai_client]
