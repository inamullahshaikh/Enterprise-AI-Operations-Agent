"""End-to-end coverage of the Phase 3 "done when" (docs/system-design.md section 28):
"'Which subscriptions end this month?' works against the demo DB." Installs a real `postgres`
connector (via the actual HTTP install endpoint, so that path gets exercised too) pointing at
a `demo_subscriptions` table seeded into the *same* test Postgres container — a separate table,
not Relay's own — then drives a full task run through `execute_step` -> `validate_step` ->
`next_step` -> `synthesize` with a scripted executor turn that calls the connector's real
`run_sql` tool against real data.

The Gemini client is scripted the same way `test_chat_flow.py` scripts it, extended with a
function-call response builder (`_function_call_response`) since Phase 2's scripts never needed
one — nothing there called a tool.
"""

import uuid

import pytest
import pytest_asyncio
from google.genai import types
from httpx import AsyncClient
from langgraph.checkpoint.memory import MemorySaver
from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_run_dispatcher
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


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def demo_subscriptions_table(migrated_db_url: str) -> None:
    """A tiny fixture table living in the *same* test Postgres container as Relay's own
    tables — a real deployment's demo DB is a physically separate database
    (docker-compose.yml's `demo-db` service), but for this test the important thing is that
    `PostgresConnector` opens its own independent `asyncpg` connection and queries real,
    committed data, not that it's isolated from Relay's own schema.

    Written with a raw `asyncpg` connection in autocommit mode, not the `db_session` fixture:
    `db_session` wraps each test in a savepoint that's rolled back at teardown, which would
    make this table (and its data) invisible to `PostgresConnector`'s own separate connection
    even within the same test.
    """
    import asyncpg

    dsn = migrated_db_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS demo_subscriptions (
                id serial PRIMARY KEY,
                account_name text,
                end_date date,
                mrr_usd numeric(10, 2)
            )
            """
        )
        await conn.execute("DELETE FROM demo_subscriptions")
        await conn.execute(
            """
            INSERT INTO demo_subscriptions (account_name, end_date, mrr_usd) VALUES
                ('Acme Robotics', date_trunc('month', CURRENT_DATE)::date + 9, 4200.00),
                ('Far Future Co', CURRENT_DATE + 200, 900.00)
            """
        )
    finally:
        await conn.close()


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


def _function_call_response(name: str, args: dict) -> types.GenerateContentResponse:
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
    """Unlike `test_chat_flow.py`'s version, this takes pre-built responses (so a step can
    script a function-call turn), not raw JSON strings to wrap as text."""

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


def _scripted_gateway(
    db_session: AsyncSession, redis_client, test_settings, *, responses, stream_text
) -> LLMGateway:
    client = _ScriptedClient(responses, stream_text)
    limiter = RedisRateLimiter(redis_client, rpm_limit=test_settings.gemini_rpm_limit)
    return LLMGateway(
        client,
        limiter=limiter,
        llm_calls=LLMCallRepository(db_session),
        pricing=ModelPricingRepository(db_session),
    )


def _install_dispatcher(*, db_session, redis_client, test_settings, gateway) -> None:
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
        )

    app.dependency_overrides[get_run_dispatcher] = lambda: _dispatch


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


async def test_task_completes_using_a_real_postgres_connector(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    demo_subscriptions_table: None,
    postgres_url: str,
) -> None:
    headers, workspace_id, conversation_id = await _register_workspace_and_conversation(
        client, "execute-step@example.com"
    )

    url = make_url(postgres_url)
    install_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json={
            "connector_key": "postgres",
            "name": "Demo DB",
            "config": {
                "host": url.host,
                "port": url.port,
                "database": url.database,
                "schemas": ["public"],
                "statement_timeout_s": 10,
                "row_limit": 500,
            },
            "secrets": {"username": url.username, "password": url.password},
        },
        headers=headers,
    )
    assert install_resp.status_code == 201, install_resp.text
    installation = install_resp.json()
    assert installation["health"] == "healthy", installation
    assert installation["slug"] == "demo-db"

    plan = Plan(
        objective="Find subscriptions ending this month",
        steps=[
            PlanStep(
                id="s1",
                goal="Look up subscriptions ending this month",
                required_capabilities=["subscription.read"],
                expected_output="account names and end dates",
            )
        ],
    )
    sql = (
        "SELECT account_name, end_date FROM demo_subscriptions "
        "WHERE date_trunc('month', end_date) = date_trunc('month', CURRENT_DATE)"
    )
    responses = [
        _text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
        _text_response(RouteVerdict(route="task").model_dump_json()),
        _text_response(plan.model_dump_json()),
        _function_call_response("demo-db__run_sql", {"sql": sql}),
        _text_response("Acme Robotics' subscription ends this month; Far Future Co's does not."),
        _text_response(
            StepVerdict(status="pass", reason="Matches the demo data.").model_dump_json()
        ),
        # `validate_final` (Phase 7 C2) checks the synthesized answer before it ships.
        _text_response(
            FinalVerdict(status="pass", reason="Grounded in the results.").model_dump_json()
        ),
    ]
    gateway = _scripted_gateway(
        db_session,
        redis_client,
        test_settings,
        responses=responses,
        stream_text="Acme Robotics is renewing this month; Far Future Co is not.",
    )
    _install_dispatcher(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=gateway,
    )
    try:
        send_resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
            json={"content": "Which subscriptions end this month?"},
            headers=headers,
        )
        assert send_resp.status_code == 202, send_resp.text
        run_id = send_resp.json()["run_id"]

        run_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers
        )
        body = run_resp.json()
        assert body["status"] == "completed", body
        assert body["route"] == "task"
        assert body["plan"]["steps"][0]["status"] == "done"
        assert body["tool_calls"] == 1

        messages_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
            headers=headers,
        )
        final = messages_resp.json()[-1]
        assert final["content"] == "Acme Robotics is renewing this month; Far Future Co is not."
    finally:
        del app.dependency_overrides[get_run_dispatcher]
