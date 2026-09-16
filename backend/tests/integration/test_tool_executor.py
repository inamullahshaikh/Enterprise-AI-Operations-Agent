"""Coverage for `relay_core.tools.executor.ToolExecutor` (docs/system-design.md section 8.7),
which `execute_step` relies on but nothing tests directly: JSON-Schema argument validation
before a connector is ever called, retrying only idempotent tools on retryable errors, never
retrying non-idempotent ones, and turning every connector failure into `ToolResult(ok=False)`
rather than letting it propagate. Uses a real `db_session`/`redis_client` (matching every other
integration test's conventions) with a small in-process fake `Connector` standing in for a real
one, since the retry/validation logic under test doesn't care which connector it's wrapping.

`tool_calls.run_id` is a real foreign key to `agent_runs`, so each test creates a real run (via
the HTTP client + `AgentRunRepository`, the same way the agent runner itself would) rather than
pointing at a bare random UUID.
"""

import uuid
from typing import Any, NamedTuple

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    Risk,
    ToolResult,
    ToolSpec,
)
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.events.publisher import EventPublisher
from relay_core.tools.executor import ToolExecutor
from relay_core.tools.registry import BoundTool

pytestmark = pytest.mark.asyncio


class _RunCtx(NamedTuple):
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    run_id: uuid.UUID


class _ScriptedConnector(Connector):
    key = "fake"
    display_name = "Fake"
    auth_type = AuthType.NONE

    def __init__(self, behaviors: list[Any]) -> None:
        # Each behavior is either a `ToolResult` to return or an `Exception` instance to raise.
        self._behaviors = list(behaviors)
        self.calls = 0

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        return []

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        self.calls += 1
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        return True, "ok"


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def run_ctx(
    client: AsyncClient, db_session: AsyncSession, request: pytest.FixtureRequest
) -> _RunCtx:
    email = f"{request.node.name.replace('[', '-').replace(']', '')}@example.com"
    register_resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert register_resp.status_code == 201, register_resp.text
    headers = {"Authorization": f"Bearer {register_resp.json()['access_token']}"}
    user_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=headers)).json()["id"])

    ws_resp = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
    workspace_id = uuid.UUID(ws_resp.json()["id"])
    conv_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={}, headers=headers
    )
    conversation_id = uuid.UUID(conv_resp.json()["id"])

    run = await AgentRunRepository(db_session).create(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        user_id=user_id,
        trigger_message_id=None,
    )
    return _RunCtx(workspace_id, user_id, conversation_id, run.id)


def _bound(connector: Connector, run_ctx: _RunCtx, *, idempotent: bool) -> BoundTool:
    return BoundTool(
        llm_name="fake__do_thing",
        installation_id=None,
        connector=connector,
        ctx=ExecutionContext(
            workspace_id=run_ctx.workspace_id,
            user_id=run_ctx.user_id,
            run_id=run_ctx.run_id,
            conversation_id=run_ctx.conversation_id,
            installation_id="fake",
        ),
        spec=ToolSpec(
            name="do_thing",
            description="Does a thing.",
            input_schema={
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            risk=Risk.READ,
            idempotent=idempotent,
        ),
    )


async def test_invalid_args_never_calls_the_connector_or_records_a_tool_call(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    connector = _ScriptedConnector([])
    bound = _bound(connector, run_ctx, idempotent=True)
    executor = ToolExecutor(ToolCallRepository(db_session), EventPublisher(redis_client))

    result = await executor.run(
        workspace_id=run_ctx.workspace_id,
        run_id=run_ctx.run_id,
        plan_step_id="s1",
        bound=bound,
        args={},  # missing required "id"
    )

    assert result.ok is False
    assert "Invalid arguments" in (result.error or "")
    assert connector.calls == 0
    count = await ToolCallRepository(db_session).count_for_run(run_ctx.workspace_id, run_ctx.run_id)
    assert count == 0


async def test_success_records_a_succeeded_tool_call(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    connector = _ScriptedConnector([ToolResult(ok=True, content={"answer": 42})])
    bound = _bound(connector, run_ctx, idempotent=True)
    executor = ToolExecutor(ToolCallRepository(db_session), EventPublisher(redis_client))

    result = await executor.run(
        workspace_id=run_ctx.workspace_id,
        run_id=run_ctx.run_id,
        plan_step_id="s1",
        bound=bound,
        args={"id": "x"},
    )

    assert result.ok is True
    assert result.content == {"answer": 42}
    calls = await ToolCallRepository(db_session).list_for_run(run_ctx.workspace_id, run_ctx.run_id)
    assert len(calls) == 1
    assert calls[0].status == "succeeded"


async def test_idempotent_tool_retries_a_retryable_error_and_then_succeeds(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    connector = _ScriptedConnector(
        [TimeoutError("slow"), TimeoutError("slow again"), ToolResult(ok=True, content="done")]
    )
    bound = _bound(connector, run_ctx, idempotent=True)
    executor = ToolExecutor(ToolCallRepository(db_session), EventPublisher(redis_client))

    result = await executor.run(
        workspace_id=run_ctx.workspace_id,
        run_id=run_ctx.run_id,
        plan_step_id="s1",
        bound=bound,
        args={"id": "x"},
    )

    assert result.ok is True
    assert connector.calls == 3


async def test_idempotent_tool_exhausts_retries_and_becomes_a_tool_error(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    connector = _ScriptedConnector([TimeoutError("1"), TimeoutError("2"), TimeoutError("3")])
    bound = _bound(connector, run_ctx, idempotent=True)
    executor = ToolExecutor(ToolCallRepository(db_session), EventPublisher(redis_client))

    result = await executor.run(
        workspace_id=run_ctx.workspace_id,
        run_id=run_ctx.run_id,
        plan_step_id="s1",
        bound=bound,
        args={"id": "x"},
    )

    assert result.ok is False
    assert "Tool call failed" in (result.error or "")
    assert connector.calls == 3


async def test_non_idempotent_tool_never_retries(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    connector = _ScriptedConnector([TimeoutError("slow")])
    bound = _bound(connector, run_ctx, idempotent=False)
    executor = ToolExecutor(ToolCallRepository(db_session), EventPublisher(redis_client))

    result = await executor.run(
        workspace_id=run_ctx.workspace_id,
        run_id=run_ctx.run_id,
        plan_step_id="s1",
        bound=bound,
        args={"id": "x"},
    )

    assert result.ok is False
    assert connector.calls == 1


async def test_a_non_retryable_exception_is_not_retried(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    connector = _ScriptedConnector([ValueError("boom")])
    bound = _bound(connector, run_ctx, idempotent=True)
    executor = ToolExecutor(ToolCallRepository(db_session), EventPublisher(redis_client))

    result = await executor.run(
        workspace_id=run_ctx.workspace_id,
        run_id=run_ctx.run_id,
        plan_step_id="s1",
        bound=bound,
        args={"id": "x"},
    )

    assert result.ok is False
    assert "boom" in (result.error or "")
    assert connector.calls == 1


async def test_a_connector_returned_failure_is_recorded_as_a_failed_tool_call(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    connector = _ScriptedConnector([ToolResult(ok=False, error="upstream rejected the request")])
    bound = _bound(connector, run_ctx, idempotent=True)
    executor = ToolExecutor(ToolCallRepository(db_session), EventPublisher(redis_client))

    result = await executor.run(
        workspace_id=run_ctx.workspace_id,
        run_id=run_ctx.run_id,
        plan_step_id="s1",
        bound=bound,
        args={"id": "x"},
    )

    assert result.ok is False
    calls = await ToolCallRepository(db_session).list_for_run(run_ctx.workspace_id, run_ctx.run_id)
    assert calls[0].status == "failed"
    assert calls[0].error == "upstream rejected the request"
