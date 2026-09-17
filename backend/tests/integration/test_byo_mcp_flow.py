"""Phase 6 "done when" (docs/system-design.md section 28): plug in the sample MCP server and the
agent uses it with no code changes.

Everything happens through the API, the way an admin would do it: install the `mcp` connector,
let the (scripted) tagger propose capabilities, lower one tool's risk after review, then chat.
Gemini is scripted, as in `test_execute_step_flow.py`; the MCP server, SSRF guard, sync, registry,
approval gate and executor are all real.

**This file imports nothing ticketing-specific from `relay_core`.** That absence is the claim.
"""

import json
import uuid
from typing import Any

import httpx
import pytest
import pytest_asyncio
from google.genai import types
from httpx import AsyncClient
from langgraph.checkpoint.memory import MemorySaver
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_genai_client, get_resume_dispatcher, get_run_dispatcher
from relay_api.main import app
from relay_core.agent.nodes.guard_input import GuardVerdict
from relay_core.agent.nodes.route import RouteVerdict
from relay_core.agent.nodes.validate_final import FinalVerdict
from relay_core.agent.nodes.validate_step import StepVerdict
from relay_core.agent.runner import resume_agent_once, run_agent_once
from relay_core.agent.state import Plan, PlanStep
from relay_core.db.repositories.llm_calls import LLMCallRepository, ModelPricingRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.ratelimit import RedisRateLimiter

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("ssrf_allows_localhost")]


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


def _response(part: types.Part) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[part]),
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


def _text(text: str) -> types.GenerateContentResponse:
    return _response(types.Part(text=text))


def _call(name: str, args: dict[str, Any]) -> types.GenerateContentResponse:
    return _response(types.Part(function_call=types.FunctionCall(name=name, args=args)))


class _ScriptedModels:
    """One script for the whole test: the install-time tagger call first, then both turns."""

    def __init__(self, responses: list[types.GenerateContentResponse]) -> None:
        self.responses = responses

    async def generate_content(self, *, model, contents, config):
        return self.responses.pop(0)

    async def generate_content_stream(self, *, model, contents, config):
        async def _gen():
            yield _text("Here is what I found.")

        return _gen()

    async def embed_content(self, *, model, contents, config):
        raise RuntimeError("no embeddings in this test")


class _ScriptedClient:
    def __init__(self, models: _ScriptedModels) -> None:
        self.aio = type("_Aio", (), {"models": models})()


def _tag(name: str, capability: str, risk: str) -> dict[str, Any]:
    return {"name": name, "capabilities": [capability], "suggested_risk": risk, "confidence": 0.95}


def _plan(goal: str, capability: str) -> str:
    step = PlanStep(id="s1", goal=goal, required_capabilities=[capability], expected_output="done")
    return Plan(objective=goal, steps=[step]).model_dump_json()


async def test_an_unseen_mcp_server_is_plannable_and_callable_with_no_code_changes(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    mcp_ticketing,
) -> None:
    mcp_url, _ = mcp_ticketing
    tagger = {
        "tools": [
            _tag("search_tickets", "custom.ticket.read", "read"),
            _tag("get_ticket", "custom.ticket.read", "read"),
            _tag("create_ticket", "custom.ticket.write", "write"),
            _tag("add_comment", "custom.ticket.write", "write"),
        ]
    }
    allow = GuardVerdict(verdict="allow", reason="").model_dump_json()
    task = RouteVerdict(route="task").model_dump_json()
    passed = StepVerdict(status="pass", reason="ok").model_dump_json()
    # `validate_final` (Phase 7 C2) checks each synthesized answer before it ships.
    grounded = FinalVerdict(status="pass", reason="Grounded in the step results.").model_dump_json()
    models = _ScriptedModels(
        [
            _text(json.dumps(tagger)),
            # Turn 1: a read.
            _text(allow),
            _text(task),
            _text(_plan("Find open P1 tickets for Acme", "custom.ticket.read")),
            _call("ticketing__search_tickets", {"account_name": "Acme", "priority": "P1"}),
            _text("Acme has two open P1 tickets."),
            _text(passed),
            _text(grounded),
            # Turn 2: a write, which stops for approval.
            _text(allow),
            _text(task),
            _text(_plan("Open a P2 ticket for Globex", "custom.ticket.write")),
            _call(
                "ticketing__create_ticket",
                {
                    "account_name": "Globex",
                    "subject": "Seat count question",
                    "priority": "P2",
                    "body": "Globex asked about seats.",
                },
            ),
            _text("Opened the ticket."),
            _text(passed),
            _text(grounded),
        ]
    )
    scripted = _ScriptedClient(models)
    gateway = LLMGateway(
        scripted,  # type: ignore[arg-type]
        limiter=RedisRateLimiter(redis_client, rpm_limit=test_settings.gemini_rpm_limit),
        llm_calls=LLMCallRepository(db_session),
        pricing=ModelPricingRepository(db_session),
    )
    checkpointer = MemorySaver()

    async def _run(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
        await run_agent_once(
            workspace_id,
            run_id,
            session=db_session,
            redis=redis_client,
            settings=test_settings,
            checkpointer=checkpointer,
            gateway=gateway,
        )

    async def _resume(workspace_id: uuid.UUID, run_id: uuid.UUID, decision: dict) -> None:
        await resume_agent_once(
            workspace_id,
            run_id,
            decision,
            session=db_session,
            redis=redis_client,
            settings=test_settings,
            checkpointer=checkpointer,
            gateway=gateway,
        )

    app.dependency_overrides[get_genai_client] = lambda: scripted
    app.dependency_overrides[get_run_dispatcher] = lambda: _run
    app.dependency_overrides[get_resume_dispatcher] = lambda: _resume
    try:
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "byo-mcp@example.com",
                "password": "correct horse battery staple",
                "full_name": "Admin",
            },
        )
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        ws = (await client.post("/api/v1/workspaces", json={"name": "BYO"}, headers=headers)).json()
        api = f"/api/v1/workspaces/{ws['id']}"

        # 1. Plug in the server.
        resp = await client.post(
            f"{api}/connectors",
            json={"connector_key": "mcp", "name": "Ticketing", "config": {"url": f"{mcp_url}/mcp"}},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["health"] == "healthy", resp.json()

        # 2. The tagger placed every tool; every one is still a write.
        tools = {t["name"]: t for t in (await client.get(f"{api}/tools", headers=headers)).json()}
        assert tools["search_tickets"]["capabilities"] == ["custom.ticket.read"]
        assert tools["create_ticket"]["capabilities"] == ["custom.ticket.write"]
        assert {t["risk"] for t in tools.values()} == {"write"}

        # An admin reviews the search tool and lowers it to a read.
        resp = await client.patch(
            f"{api}/tools/{tools['search_tickets']['id']}",
            json={"risk": "read", "reviewed": True},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        # 3. The custom capability is available to plan against.
        capabilities = (await client.get(f"{api}/capabilities", headers=headers)).json()
        [ticket_read] = [
            c for c in capabilities["capabilities"] if c["capability"] == "custom.ticket.read"
        ]
        assert ticket_read["available"] is True

        # 4. A read runs straight through against the real server.
        conversation = (await client.post(f"{api}/conversations", json={}, headers=headers)).json()
        messages = f"{api}/conversations/{conversation['id']}/messages"
        resp = await client.post(
            messages, json={"content": "Which P1 tickets does Acme have open?"}, headers=headers
        )
        assert resp.status_code == 202, resp.text
        run_id = uuid.UUID(resp.json()["run_id"])
        run = (await client.get(f"{api}/runs/{run_id}", headers=headers)).json()
        assert run["status"] == "completed", run
        [search] = await ToolCallRepository(db_session).list_for_run(uuid.UUID(ws["id"]), run_id)
        assert search.status == "succeeded"
        subjects = {t["subject"] for t in search.output["content"]["result"]}
        assert subjects == {"Fleet dashboard not loading", "SSO login loop after renewal"}

        # 5. A write stops for approval, and nothing reaches the server until it's approved.
        async with httpx.AsyncClient(base_url=mcp_url) as ticketing:

            async def tickets_created() -> int:
                return (await ticketing.get("/_stats")).json()["tickets_created"]

            resp = await client.post(
                messages,
                json={"content": "Open a P2 ticket for Globex about seats"},
                headers=headers,
            )
            run_id = uuid.UUID(resp.json()["run_id"])
            run = (await client.get(f"{api}/runs/{run_id}", headers=headers)).json()
            assert run["status"] == "awaiting_approval", run
            assert await tickets_created() == 0

            [approval] = (await client.get(f"{api}/approvals", headers=headers)).json()
            resp = await client.post(
                f"{api}/approvals/{approval['id']}/decision",
                json={"action": "approve"},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            assert await tickets_created() == 1

        run = (await client.get(f"{api}/runs/{run_id}", headers=headers)).json()
        assert run["status"] == "completed", run
        assert models.responses == []
    finally:
        for dependency in (get_genai_client, get_run_dispatcher, get_resume_dispatcher):
            app.dependency_overrides.pop(dependency, None)
