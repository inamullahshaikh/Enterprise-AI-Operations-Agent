"""A Gemini client scripted with a fixed list of responses, plus the wiring that drives a whole
run against it.

`test_chat_flow.py` and `test_execute_step_flow.py` each grew their own copy of this; the graph
tests added in Phase 7 (`test_replan.py`, `test_validate_final.py`) share one instead, because
both of them need something those copies don't have: a *different* streamed text per call, so a
first draft and its revision can be told apart.
"""

import uuid
from collections.abc import Awaitable, Callable

from google.genai import types
from httpx import AsyncClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_run_dispatcher
from relay_api.main import app
from relay_core.agent.runner import run_agent_once
from relay_core.db.repositories.llm_calls import LLMCallRepository, ModelPricingRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.ratelimit import RedisRateLimiter


def text_response(text: str) -> types.GenerateContentResponse:
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


class ScriptedModels:
    """`responses` answers `generate_content` in order; `stream_texts` answers
    `generate_content_stream` in order, each one emitted as two chunks so a test can tell a
    buffered draft from a single published blob.

    `systems` and `contents` record the system instruction and the user turns of every call,
    streamed or not. That is what the Phase 7 memory tests assert on: a memory or a summary that
    never reaches a prompt has changed nothing, however faithfully it was retrieved."""

    def __init__(self, responses: list[types.GenerateContentResponse], stream_texts: list[str]):
        self.responses = list(responses)
        self.stream_texts = list(stream_texts)
        self.calls: list[str] = []
        self.systems: list[str] = []
        self.contents: list[object] = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append("generate")
        self.systems.append(config.system_instruction or "")
        self.contents.append(contents)
        return self.responses.pop(0)

    async def embed_content(self, *, model, contents, config):
        """`load_context` embeds every user message to retrieve memories, so a scripted client
        that could not embed would make the retrieval failure path the one every flow test
        takes. The vectors are a fixed non-answer: a test that cares which memory comes back
        replaces `gateway.embed` outright."""
        return types.EmbedContentResponse(
            embeddings=[types.ContentEmbedding(values=[0.0] * 768) for _ in contents]
        )

    async def generate_content_stream(self, *, model, contents, config):
        self.calls.append("stream")
        self.systems.append(config.system_instruction or "")
        self.contents.append(contents)
        text = self.stream_texts.pop(0)
        head, tail = text[: len(text) // 2], text[len(text) // 2 :]

        async def _gen():
            yield text_response(head)
            yield text_response(tail)

        return _gen()


class ScriptedClient:
    def __init__(self, models: ScriptedModels) -> None:
        self.aio = _Aio(models)


class _Aio:
    def __init__(self, models: ScriptedModels) -> None:
        self.models = models


def scripted_gateway(
    db_session: AsyncSession,
    redis_client,
    test_settings,
    *,
    responses: list[types.GenerateContentResponse],
    stream_texts: list[str],
) -> tuple[LLMGateway, ScriptedModels]:
    models = ScriptedModels(responses, stream_texts)
    return (
        LLMGateway(
            ScriptedClient(models),
            limiter=RedisRateLimiter(redis_client, rpm_limit=test_settings.gemini_rpm_limit),
            llm_calls=LLMCallRepository(db_session),
            pricing=ModelPricingRepository(db_session),
        ),
        models,
    )


def install_dispatcher(
    *, db_session, redis_client, test_settings, gateway, extract_memories=None
) -> Callable[[], None]:
    """Returns the teardown, so a test can `try: ... finally: teardown()` like the Phase 3
    flow tests do.

    `extract_memories` replaces the post-run memory enqueue. The real one hangs off the session's
    `after_commit`, which never fires here (every test transaction is rolled back), so a test
    that wants to prove `finalize` asks for extraction has to pass its own."""
    checkpointer = MemorySaver()

    async def _dispatch(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
        await run_agent_once(
            workspace_id,
            run_id,
            session=db_session,
            redis=redis_client,
            settings=test_settings,
            checkpointer=checkpointer,
            gateway=gateway,
            extract_memories=extract_memories,
        )

    app.dependency_overrides[get_run_dispatcher] = lambda: _dispatch
    return lambda: app.dependency_overrides.pop(get_run_dispatcher, None)


async def register_workspace_and_conversation(
    client: AsyncClient, email: str
) -> tuple[dict[str, str], uuid.UUID, uuid.UUID]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert resp.status_code == 201, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    ws = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
    assert ws.status_code == 201, ws.text
    workspace_id = uuid.UUID(ws.json()["id"])

    conv = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={}, headers=headers
    )
    assert conv.status_code == 201, conv.text
    return headers, workspace_id, uuid.UUID(conv.json()["id"])


async def send_and_wait(
    client: AsyncClient,
    headers: dict[str, str],
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    content: str,
) -> dict:
    """Posts a message (which runs the graph inline through the dispatcher override) and returns
    the finished run."""
    send = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": content},
        headers=headers,
    )
    assert send.status_code == 202, send.text
    run = await client.get(
        f"/api/v1/workspaces/{workspace_id}/runs/{send.json()['run_id']}", headers=headers
    )
    assert run.status_code == 200, run.text
    return run.json()


async def published_events(redis_client, run_id: str) -> list[dict]:
    """Everything published to this run's stream, decoded. `EventPublisher` writes to
    `run:{run_id}`; reading it back is how a test asserts what the user would have seen."""
    entries = await redis_client.xrange(f"run:{run_id}")
    return [
        {key.decode(): value.decode() for key, value in fields.items()} for _, fields in entries
    ]


AwaitableNone = Awaitable[None]
