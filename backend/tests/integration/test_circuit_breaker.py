"""The per-installation circuit breaker (Phase 7 E1, docs/system-design.md section 19.1).

Two properties matter and both are easy to build something that only looks like it has them.
The first is that an open breaker actually stops the call: a version that opens, marks the
installation degraded and then calls the connector anyway would pass any test that only reads
the health column. The second is that a Redis outage is harmless — a breaker that raises when
it cannot be read would turn one failing dependency into a total outage, which is strictly
worse than having no breaker at all.
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
from relay_core.connectors.breaker import FAILURE_THRESHOLD, CircuitBreaker
from relay_core.db.models.tools import ToolDefinition
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.events.publisher import EventPublisher
from relay_core.tools.executor import ToolExecutor
from relay_core.tools.registry import BoundTool

pytestmark = pytest.mark.asyncio


class _RunCtx(NamedTuple):
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    run_id: uuid.UUID


class _AlwaysFails(Connector):
    key = "fake"
    display_name = "Fake"
    auth_type = AuthType.NONE

    def __init__(self) -> None:
        self.calls = 0

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        return []

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        self.calls += 1
        raise ConnectionError("upstream is down")

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        return False, "down"


class _CleanBusinessError(_AlwaysFails):
    """Returns a failure the *connector* produced, rather than raising. A breaker must not count
    these — the connector answered."""

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        self.calls += 1
        return ToolResult(ok=False, error="No account with that id")


class _BrokenRedis:
    async def incr(self, *a: Any, **k: Any) -> int:
        raise ConnectionError("redis is gone")

    async def expire(self, *a: Any, **k: Any) -> bool:
        raise ConnectionError("redis is gone")

    async def exists(self, *a: Any, **k: Any) -> int:
        raise ConnectionError("redis is gone")

    async def mget(self, *a: Any, **k: Any) -> list[Any]:
        raise ConnectionError("redis is gone")

    async def set(self, *a: Any, **k: Any) -> bool:
        raise ConnectionError("redis is gone")

    async def delete(self, *a: Any, **k: Any) -> int:
        raise ConnectionError("redis is gone")


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
    email = f"breaker-{uuid.uuid4().hex[:8]}@example.com"
    register = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert register.status_code == 201, register.text
    headers = {"Authorization": f"Bearer {register.json()['access_token']}"}
    user_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=headers)).json()["id"])

    ws = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
    workspace_id = uuid.UUID(ws.json()["id"])
    conv = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={}, headers=headers
    )
    conversation_id = uuid.UUID(conv.json()["id"])
    run = await AgentRunRepository(db_session).create(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        user_id=user_id,
        trigger_message_id=None,
    )
    return _RunCtx(workspace_id, user_id, conversation_id, run.id)


async def _installation(
    session: AsyncSession, run_ctx: _RunCtx, slug: str, *, priority: int
) -> uuid.UUID:
    installation = await ConnectorInstallationRepository(session).create(
        workspace_id=run_ctx.workspace_id,
        connector_key="mcp",
        name=slug,
        slug=slug,
        config={},
        priority=priority,
        installed_by=run_ctx.user_id,
    )
    await session.flush()
    await ConnectorInstallationRepository(session).set_health(
        run_ctx.workspace_id, installation.id, health="healthy", message="ok", status="active"
    )
    await ToolDefinitionRepository(session).add(
        ToolDefinition(
            workspace_id=run_ctx.workspace_id,
            installation_id=installation.id,
            name="search_tickets",
            llm_name=f"{slug}__search_tickets",
            description="Search tickets",
            input_schema={"type": "object"},
            schema_hash="x",
            risk="read",
            capabilities=["custom.ticket.read"],
        )
    )
    await session.flush()
    return installation.id


def _bound(connector: Connector, run_ctx: _RunCtx, installation_id: uuid.UUID | None) -> BoundTool:
    return BoundTool(
        llm_name="fake__do_thing",
        installation_id=installation_id,
        connector=connector,
        ctx=ExecutionContext(
            workspace_id=run_ctx.workspace_id,
            user_id=run_ctx.user_id,
            run_id=run_ctx.run_id,
            conversation_id=run_ctx.conversation_id,
            installation_id=str(installation_id),
        ),
        spec=ToolSpec(
            name="do_thing",
            description="Does a thing.",
            input_schema={"type": "object", "properties": {}},
            risk=Risk.READ,
            idempotent=False,
            timeout_s=5.0,
        ),
    )


async def _call(executor: ToolExecutor, run_ctx: _RunCtx, bound: BoundTool) -> ToolResult:
    return await executor.run(
        workspace_id=run_ctx.workspace_id,
        run_id=run_ctx.run_id,
        plan_step_id="s1",
        bound=bound,
        args={},
    )


async def test_five_failures_open_the_breaker_and_the_sixth_call_never_reaches_the_connector(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    installation_id = await _installation(db_session, run_ctx, "flaky", priority=100)
    connector = _AlwaysFails()
    bound = _bound(connector, run_ctx, installation_id)
    executor = ToolExecutor(
        ToolCallRepository(db_session),
        EventPublisher(redis_client),
        CircuitBreaker(redis_client),
        ConnectorInstallationRepository(db_session),
    )

    for _ in range(FAILURE_THRESHOLD):
        assert (await _call(executor, run_ctx, bound)).ok is False
    assert connector.calls == FAILURE_THRESHOLD

    blocked = await _call(executor, run_ctx, bound)

    assert blocked.ok is False
    assert "taken out of service" in (blocked.error or "")
    assert connector.calls == FAILURE_THRESHOLD, "the sixth call reached the connector"

    installation = await ConnectorInstallationRepository(db_session).get(
        run_ctx.workspace_id, installation_id
    )
    assert installation is not None
    assert installation.health == "degraded"
    assert "Circuit breaker open" in (installation.health_message or "")


async def test_a_clean_business_error_never_opens_the_breaker(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    """The connector answered; it just answered "no". Counting that would open the breaker on a
    perfectly healthy connector being asked six questions with no answer."""
    installation_id = await _installation(db_session, run_ctx, "polite", priority=100)
    connector = _CleanBusinessError()
    bound = _bound(connector, run_ctx, installation_id)
    executor = ToolExecutor(
        ToolCallRepository(db_session),
        EventPublisher(redis_client),
        CircuitBreaker(redis_client),
        ConnectorInstallationRepository(db_session),
    )

    for _ in range(FAILURE_THRESHOLD + 2):
        assert (await _call(executor, run_ctx, bound)).ok is False

    assert connector.calls == FAILURE_THRESHOLD + 2
    assert await CircuitBreaker(redis_client).is_open(installation_id) is False


async def test_an_open_installation_is_skipped_and_the_next_one_binds(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    """Decision 7: an open breaker is an unhealthy installation as far as binding is concerned,
    so the capability falls to the next by priority and the planner needs no change."""
    from relay_core.capabilities.resolver import drop_tripped

    best = await _installation(db_session, run_ctx, "primary", priority=10)
    await _installation(db_session, run_ctx, "backup", priority=50)
    repo = ToolDefinitionRepository(db_session)
    breaker = CircuitBreaker(redis_client)

    rows = await drop_tripped(await repo.list_bindable(run_ctx.workspace_id), breaker)
    assert [r.llm_name for r, _ in rows] == [
        "primary__search_tickets",
        "backup__search_tickets",
    ]

    for _ in range(FAILURE_THRESHOLD):
        await breaker.record_failure(best)

    rows = await drop_tripped(await repo.list_bindable(run_ctx.workspace_id), breaker)

    assert [r.llm_name for r, _ in rows] == ["backup__search_tickets"]
    # The capability is still available — from the backup, at no cost to the planner.
    assert "custom.ticket.read" in {c for r, _ in rows for c in r.capabilities}


async def test_the_breaker_closes_again_when_its_window_expires(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    """`OPEN_S` is two minutes, which no test should wait for. Expiring the key is exactly what
    Redis does at the end of the window, so dropping it early is the same event, sooner."""
    from relay_core.capabilities.resolver import drop_tripped
    from relay_core.connectors.breaker import _open_key

    installation_id = await _installation(db_session, run_ctx, "recovering", priority=10)
    breaker = CircuitBreaker(redis_client)
    for _ in range(FAILURE_THRESHOLD):
        await breaker.record_failure(installation_id)
    assert await breaker.is_open(installation_id) is True

    repo = ToolDefinitionRepository(db_session)
    assert await drop_tripped(await repo.list_bindable(run_ctx.workspace_id), breaker) == []

    await redis_client.delete(_open_key(installation_id))

    assert await breaker.is_open(installation_id) is False
    rows = await drop_tripped(await repo.list_bindable(run_ctx.workspace_id), breaker)
    assert [r.llm_name for r, _ in rows] == ["recovering__search_tickets"]


async def test_a_successful_call_clears_the_count_and_the_health_message(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    installation_id = await _installation(db_session, run_ctx, "flappy", priority=10)
    breaker = CircuitBreaker(redis_client)
    for _ in range(FAILURE_THRESHOLD):
        await breaker.record_failure(installation_id)
    await ConnectorInstallationRepository(db_session).set_health(
        run_ctx.workspace_id, installation_id, health="degraded", message="Circuit breaker open"
    )

    class _Works(_AlwaysFails):
        async def call_tool(
            self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
        ) -> ToolResult:
            self.calls += 1
            return ToolResult(ok=True, content=[{"found": 1}])

    # The breaker is open, so it has to be cleared before a call can get through at all — which
    # is what the recovery sweep does on a successful health check.
    from relay_core.connectors.breaker import _open_key

    await redis_client.delete(_open_key(installation_id))

    executor = ToolExecutor(
        ToolCallRepository(db_session),
        EventPublisher(redis_client),
        breaker,
        ConnectorInstallationRepository(db_session),
    )
    result = await _call(executor, run_ctx, _bound(_Works(), run_ctx, installation_id))

    assert result.ok is True
    installation = await ConnectorInstallationRepository(db_session).get(
        run_ctx.workspace_id, installation_id
    )
    assert installation is not None
    assert installation.health == "healthy"


async def test_a_redis_outage_leaves_tool_calls_working(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    """A breaker that cannot be read is closed (section 19.1). The call still has to happen."""
    installation_id = await _installation(db_session, run_ctx, "unreadable", priority=10)

    class _Works(_AlwaysFails):
        async def call_tool(
            self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
        ) -> ToolResult:
            self.calls += 1
            return ToolResult(ok=True, content=[{"ok": True}])

    connector = _Works()
    executor = ToolExecutor(
        ToolCallRepository(db_session),
        # Events go to the working Redis; only the breaker's client is broken, which is the
        # failure being modelled.
        EventPublisher(redis_client),
        CircuitBreaker(_BrokenRedis()),  # type: ignore[arg-type]
        ConnectorInstallationRepository(db_session),
    )

    result = await _call(executor, run_ctx, _bound(connector, run_ctx, installation_id))

    assert result.ok is True
    assert connector.calls == 1


async def test_binding_ignores_a_breaker_it_cannot_read(
    db_session: AsyncSession, redis_client: Redis, run_ctx: _RunCtx
) -> None:
    from relay_core.capabilities.resolver import drop_tripped

    await _installation(db_session, run_ctx, "still-binds", priority=10)
    rows = await ToolDefinitionRepository(db_session).list_bindable(run_ctx.workspace_id)

    kept = await drop_tripped(rows, CircuitBreaker(_BrokenRedis()))  # type: ignore[arg-type]

    assert [r.llm_name for r, _ in kept] == ["still-binds__search_tickets"]
