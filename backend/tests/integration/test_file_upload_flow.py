"""End-to-end coverage of the other half of the Phase 3 "done when" (docs/system-design.md
section 28): "...and via uploaded CSV." Uploads a real CSV through the real multipart endpoint,
then drives a task run that reads it back through the `file_upload` connector — no `postgres`
installation, no other connector, proving `usage.read` alone came from the attachment's
inferred capabilities (`relay_core.capabilities.resolver`).

Uses a small in-memory `FakeObjectStore` instead of real R2: `relay_core.agent.runner.
run_agent_once` and the `get_object_store` FastAPI dependency both accept an injectable
`ObjectStore`, mirroring how `gateway` is already injected for scripted-Gemini tests.
"""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from google.genai import types
from httpx import AsyncClient
from langgraph.checkpoint.memory import MemorySaver
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_object_store, get_run_dispatcher
from relay_api.main import app
from relay_core.agent.nodes.guard_input import GuardVerdict
from relay_core.agent.nodes.route import RouteVerdict
from relay_core.agent.nodes.validate_final import FinalVerdict
from relay_core.agent.nodes.validate_step import StepVerdict
from relay_core.agent.runner import run_agent_once
from relay_core.agent.state import Plan, PlanStep
from relay_core.db.repositories.llm_calls import LLMCallRepository, ModelPricingRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.ratelimit import RedisRateLimiter

pytestmark = pytest.mark.asyncio

_CSV = b"account_name,month,active_users\nAcme Robotics,2026-08,58\nGlobex,2026-08,88\n"


class FakeObjectStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        self.blobs[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.blobs[key]


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


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


def _function_call_response(name: str, args: dict[str, Any]) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
                ),
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
    def __init__(self, responses: list[types.GenerateContentResponse], stream_text: str) -> None:
        self._responses = list(responses)
        self._stream_text = stream_text

    async def generate_content(self, *, model, contents, config):
        return self._responses.pop(0)

    async def generate_content_stream(self, *, model, contents, config):
        chunk = _text_response(self._stream_text)

        async def _gen():
            yield chunk

        return _gen()


class _ScriptedClient:
    def __init__(self, responses: list[types.GenerateContentResponse], stream_text: str) -> None:
        self.aio = _Aio(_ScriptedModels(responses, stream_text))


class _Aio:
    def __init__(self, models: _ScriptedModels) -> None:
        self.models = models


async def _register_workspace_and_conversation(
    client: AsyncClient, email: str
) -> tuple[dict, uuid.UUID, uuid.UUID]:
    register_resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert register_resp.status_code == 201, register_resp.text
    token = register_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    ws_resp = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
    assert ws_resp.status_code == 201, ws_resp.text
    workspace_id = uuid.UUID(ws_resp.json()["id"])

    conv_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={}, headers=headers
    )
    assert conv_resp.status_code == 201, conv_resp.text
    conversation_id = uuid.UUID(conv_resp.json()["id"])

    return headers, workspace_id, conversation_id


async def test_task_completes_using_an_uploaded_csv_and_no_connectors(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    headers, workspace_id, conversation_id = await _register_workspace_and_conversation(
        client, "file-upload@example.com"
    )

    fake_store = FakeObjectStore()
    app.dependency_overrides[get_object_store] = lambda: fake_store
    try:
        upload_resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
            files={"file": ("usage.csv", _CSV, "text/csv")},
            headers=headers,
        )
    finally:
        del app.dependency_overrides[get_object_store]
    assert upload_resp.status_code == 201, upload_resp.text
    attachment = upload_resp.json()
    assert "usage.read" in attachment["inferred_capabilities"]
    attachment_id = attachment["id"]

    plan = Plan(
        objective="Summarize the uploaded usage export",
        steps=[
            PlanStep(
                id="s1",
                goal="Read the uploaded usage export and summarize active users per account",
                required_capabilities=["usage.read"],
                expected_output="a per-account active user count",
            )
        ],
    )
    responses = [
        _text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
        _text_response(RouteVerdict(route="task").model_dump_json()),
        _text_response(plan.model_dump_json()),
        _function_call_response(
            "file_upload__read_table", {"attachment_id": attachment_id, "limit": 200}
        ),
        _text_response("Acme Robotics: 58 active users; Globex: 88 active users."),
        _text_response(StepVerdict(status="pass", reason="Matches the CSV.").model_dump_json()),
        # `validate_final` (Phase 7 C2) checks the synthesized answer before it ships.
        _text_response(
            FinalVerdict(status="pass", reason="Grounded in the results.").model_dump_json()
        ),
    ]
    gateway = LLMGateway(
        _ScriptedClient(responses, "Acme Robotics: 58; Globex: 88."),
        limiter=RedisRateLimiter(redis_client, rpm_limit=test_settings.gemini_rpm_limit),
        llm_calls=LLMCallRepository(db_session),
        pricing=ModelPricingRepository(db_session),
    )
    checkpointer = MemorySaver()

    async def _dispatch(ws_id: uuid.UUID, run_id: uuid.UUID) -> None:
        await run_agent_once(
            ws_id,
            run_id,
            session=db_session,
            redis=redis_client,
            settings=test_settings,
            checkpointer=checkpointer,
            gateway=gateway,
            object_store=fake_store,
        )

    app.dependency_overrides[get_run_dispatcher] = lambda: _dispatch
    try:
        send_resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
            json={"content": "Using the attached usage export, how many active users per account?"},
            headers=headers,
        )
        assert send_resp.status_code == 202, send_resp.text
        run_id = send_resp.json()["run_id"]

        run_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers
        )
        body = run_resp.json()
        # The whole point: no connector was installed, yet this never hits ask_missing —
        # the attachment's inferred usage.read capability was enough.
        assert body["status"] == "completed", body
        assert body["missing_capabilities"] is None
        assert body["plan"]["steps"][0]["status"] == "done"
        assert body["tool_calls"] == 1

        messages_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
            headers=headers,
        )
        assert messages_resp.json()[-1]["content"] == "Acme Robotics: 58; Globex: 88."
    finally:
        del app.dependency_overrides[get_run_dispatcher]
