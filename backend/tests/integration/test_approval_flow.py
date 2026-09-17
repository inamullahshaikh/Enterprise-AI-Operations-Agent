"""End-to-end coverage of Phase 5's approval gate (docs/system-design.md section 13): a write
tool stops the run at `interrupt()`, a human decides through the API, and the run resumes from
its checkpoint and finishes.

**Why a stub connector.** None of the connectors that ship today expose a `write` tool — gmail
and google_calendar are the first, and they arrive later in Phase 5 — so there is nothing real
to gate yet. Rather than defer testing the mechanism until then, `ToolRegistry.tools_for_run` is
patched to hand back one recording connector whose single tool is `Risk.WRITE`. That keeps the
thing under test honest: everything from `execute_step`'s gating decision through the checkpoint,
the decision endpoint and `resume_agent_once` is the real code path, and only the tool at the end
of it is a stand-in.

The scripted-Gemini scaffolding mirrors `test_execute_step_flow.py`; the one difference that
matters is that a single `_ScriptedModels` instance spans both the initial run *and* the resume,
because the resumed run continues the same conversation with the model.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from google.genai import types
from httpx import AsyncClient
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_resume_dispatcher, get_run_dispatcher
from relay_api.main import app
from relay_core.agent.nodes.execute_step import build_function_response_content
from relay_core.agent.nodes.guard_input import GuardVerdict
from relay_core.agent.nodes.route import RouteVerdict
from relay_core.agent.nodes.validate_step import StepVerdict
from relay_core.agent.runner import resume_agent_once, run_agent_once
from relay_core.agent.state import Plan, PlanStep
from relay_core.approvals import expire_stale_approvals
from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    Risk,
    ToolResult,
    ToolSpec,
)
from relay_core.db.repositories.approvals import ApprovalRepository
from relay_core.db.repositories.llm_calls import LLMCallRepository, ModelPricingRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.events.publisher import EventPublisher
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.ratelimit import RedisRateLimiter
from relay_core.tools.registry import BoundTool, BoundToolSet, ToolRegistry

pytestmark = pytest.mark.asyncio

_TOOL = "test_email__send_email"


class _NoConfig(BaseModel):
    pass


class WorkerKilled(BaseException):
    """Stands in for SIGKILL in the durability test. Inherits `BaseException`, not `Exception`,
    so it slips past `resume_agent_once`'s top-level handler exactly as a real kill would — the
    run is never marked failed and the transaction never commits. (`KeyboardInterrupt` would be
    the natural choice, but pytest intercepts it to abort the whole session.)"""


class RecordingEmailConnector(Connector):
    """One `write` tool that records every call it receives, so a test can assert not just that
    an approved action happened but that it happened exactly once."""

    key = "test_email"
    display_name = "Test Email"
    auth_type = AuthType.NONE
    config_model = _NoConfig
    secrets_model = _NoConfig

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="send_email",
                description="Send an email.",
                input_schema={
                    "type": "object",
                    "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["to", "body"],
                },
                risk=Risk.WRITE,
                capabilities=["email.send"],
                idempotent=False,
            )
        ]

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        self.sent.append(args)
        return ToolResult(ok=True, content={"message_id": f"msg-{len(self.sent)}"})

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        return True, "ok"


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def email_connector(monkeypatch: pytest.MonkeyPatch) -> RecordingEmailConnector:
    connector = RecordingEmailConnector()

    # The capability resolver only sees connectors with a real installation row and manifest, so
    # `email.send` would otherwise read as missing and `check_capabilities` would divert the run
    # to `ask_missing` before it ever reached a tool. Patching it here stands in for having the
    # gmail connector installed.
    async def _capabilities(*_args: Any, **_kwargs: Any) -> list[str]:
        return ["file.read", "email.send"]

    monkeypatch.setattr(
        "relay_core.agent.nodes.load_context.resolve_available_capabilities", _capabilities
    )

    async def _tools_for_run(
        self: ToolRegistry,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        run_id: uuid.UUID,
        conversation_id: uuid.UUID,
        capabilities: list[str],
    ) -> BoundToolSet:
        ctx = ExecutionContext(
            workspace_id=workspace_id,
            user_id=user_id,
            run_id=run_id,
            conversation_id=conversation_id,
            installation_id="test_email",
        )
        specs = await connector.list_tools(ctx)
        return BoundToolSet(
            [
                BoundTool(
                    llm_name=f"test_email__{spec.name}",
                    installation_id=None,
                    connector=connector,
                    ctx=ctx,
                    spec=spec,
                )
                for spec in specs
            ]
        )

    monkeypatch.setattr(ToolRegistry, "tools_for_run", _tools_for_run)
    return connector


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


class _Aio:
    def __init__(self, models: _ScriptedModels) -> None:
        self.models = models


class _ScriptedClient:
    def __init__(self, responses: list[types.GenerateContentResponse], stream_text: str) -> None:
        self.aio = _Aio(_ScriptedModels(responses, stream_text))


def _plan() -> Plan:
    return Plan(
        objective="Email the renewal reminder",
        steps=[
            PlanStep(
                id="s1",
                goal="Send the renewal reminder to the account owner",
                required_capabilities=["email.send"],
                expected_output="the email was sent",
            )
        ],
    )


def _script() -> list[types.GenerateContentResponse]:
    return [
        _text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
        _text_response(RouteVerdict(route="task").model_dump_json()),
        _text_response(_plan().model_dump_json()),
        _function_call_response(_TOOL, {"to": "ops@acme.test", "body": "Your plan renews soon."}),
        # Consumed after the resume, when `execute_step` re-enters with the decision folded in.
        _text_response("Handled the renewal reminder."),
        _text_response(StepVerdict(status="pass", reason="Step resolved.").model_dump_json()),
    ]


def _install_dispatchers(*, db_session, redis_client, test_settings, gateway) -> None:
    """One `MemorySaver` shared by the run and its resume — that shared checkpoint is what the
    resume actually reads back, so a per-call saver would make the test pass for the wrong
    reason."""
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

    async def _resume(
        workspace_id: uuid.UUID, run_id: uuid.UUID, decision: dict[str, Any]
    ) -> None:
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

    app.dependency_overrides[get_run_dispatcher] = lambda: _run
    app.dependency_overrides[get_resume_dispatcher] = lambda: _resume


def _scripted_gateway(db_session, redis_client, test_settings, *, responses) -> LLMGateway:
    client = _ScriptedClient(responses, "Done — the reminder has gone out.")
    limiter = RedisRateLimiter(redis_client, rpm_limit=test_settings.gemini_rpm_limit)
    return LLMGateway(
        client,
        limiter=limiter,
        llm_calls=LLMCallRepository(db_session),
        pricing=ModelPricingRepository(db_session),
    )


async def _register(client: AsyncClient, email: str) -> tuple[dict, uuid.UUID, uuid.UUID]:
    register_resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert register_resp.status_code == 201, register_resp.text
    headers = {"Authorization": f"Bearer {register_resp.json()['access_token']}"}

    ws_resp = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
    workspace_id = uuid.UUID(ws_resp.json()["id"])
    conv_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={}, headers=headers
    )
    conversation_id = uuid.UUID(conv_resp.json()["id"])
    return headers, workspace_id, conversation_id


async def _send_and_park(
    client: AsyncClient, headers: dict, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> tuple[str, dict]:
    send_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "Email the renewal reminder to ops@acme.test"},
        headers=headers,
    )
    assert send_resp.status_code == 202, send_resp.text
    run_id = send_resp.json()["run_id"]

    run_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers
    )
    assert run_resp.json()["status"] == "awaiting_approval", run_resp.json()

    inbox = await client.get(f"/api/v1/workspaces/{workspace_id}/approvals", headers=headers)
    assert inbox.status_code == 200, inbox.text
    pending = inbox.json()
    assert len(pending) == 1, pending
    return run_id, pending[0]


async def test_write_parks_the_run_and_approving_it_resumes_to_completion(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    email_connector: RecordingEmailConnector,
) -> None:
    headers, workspace_id, conversation_id = await _register(client, "approve-yes@example.com")
    _install_dispatchers(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=_scripted_gateway(db_session, redis_client, test_settings, responses=_script()),
    )
    try:
        run_id, approval = await _send_and_park(client, headers, workspace_id, conversation_id)

        # Nothing has been sent while the run sits parked — this is the whole point of the gate.
        assert email_connector.sent == []
        assert approval["summary"] == "Approve: test_email__send_email"

        decision = await client.post(
            f"/api/v1/workspaces/{workspace_id}/approvals/{approval['id']}/decision",
            json={"action": "approve"},
            headers=headers,
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "approved"

        assert email_connector.sent == [{"to": "ops@acme.test", "body": "Your plan renews soon."}]

        run_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers
        )
        body = run_resp.json()
        assert body["status"] == "completed", body
        assert body["plan"]["steps"][0]["status"] == "done"

        calls = await ToolCallRepository(db_session).list_for_run(
            workspace_id, uuid.UUID(run_id)
        )
        assert [c.status for c in calls] == ["succeeded"]
        assert calls[0].idempotency_key is not None
    finally:
        app.dependency_overrides.pop(get_run_dispatcher, None)
        app.dependency_overrides.pop(get_resume_dispatcher, None)


async def test_rejecting_leaves_the_action_unperformed_and_still_finishes_the_run(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    email_connector: RecordingEmailConnector,
) -> None:
    """A rejection is not a crash: the model gets told the action was declined and summarizes
    around it, so the user still gets an answer."""
    headers, workspace_id, conversation_id = await _register(client, "approve-no@example.com")
    _install_dispatchers(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=_scripted_gateway(db_session, redis_client, test_settings, responses=_script()),
    )
    try:
        run_id, approval = await _send_and_park(client, headers, workspace_id, conversation_id)

        decision = await client.post(
            f"/api/v1/workspaces/{workspace_id}/approvals/{approval['id']}/decision",
            json={"action": "reject", "reason": "Wrong recipient"},
            headers=headers,
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "rejected"

        assert email_connector.sent == []

        run_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers
        )
        assert run_resp.json()["status"] == "completed", run_resp.json()

        calls = await ToolCallRepository(db_session).list_for_run(
            workspace_id, uuid.UUID(run_id)
        )
        assert [c.status for c in calls] == ["rejected"]
        assert calls[0].error == "Wrong recipient"
    finally:
        app.dependency_overrides.pop(get_run_dispatcher, None)
        app.dependency_overrides.pop(get_resume_dispatcher, None)


async def test_a_killed_worker_does_not_send_twice_when_the_run_is_resumed(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    email_connector: RecordingEmailConnector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/system-design.md section 26's graph-durability row: "Kill worker after approval,
    resume -> exactly one draft created."

    The kill is simulated where it actually hurts — *after* the connector call has happened but
    *before* the node's state reaches a checkpoint. A `BaseException` rather than a plain
    exception, because a real SIGKILL doesn't give `resume_agent_once`'s `except Exception`
    handler a chance to run either; the transaction simply never commits.

    What survives the kill is the `tool_calls` row, which `ToolExecutor` commits out of band
    precisely so it can outlive a rollback. The second resume therefore finds the call already
    succeeded and replays its stored output instead of calling the connector again.
    """
    headers, workspace_id, conversation_id = await _register(client, "kill-resume@example.com")
    checkpointer = MemorySaver()
    gateway = _scripted_gateway(db_session, redis_client, test_settings, responses=_script())

    async def _run(workspace_id_: uuid.UUID, run_id_: uuid.UUID) -> None:
        await run_agent_once(
            workspace_id_,
            run_id_,
            session=db_session,
            redis=redis_client,
            settings=test_settings,
            checkpointer=checkpointer,
            gateway=gateway,
        )

    app.dependency_overrides[get_run_dispatcher] = lambda: _run
    try:
        run_id, approval = await _send_and_park(client, headers, workspace_id, conversation_id)
        approval_id = uuid.UUID(approval["id"])

        # Decide directly rather than through the API: the endpoint would dispatch the resume
        # itself, and this test needs to drive the two resume attempts by hand.
        await ApprovalRepository(db_session).decide(
            workspace_id, approval_id, status="approved", decided_by=None
        )
        await db_session.commit()

        decision = {"action": "approve", "item_ids": [], "edited_args": {}, "reason": None}

        # Arms exactly once, rather than being reverted with `monkeypatch.undo()` between the
        # two resumes: undo() rolls back *every* patch on this fixture, including the
        # `email_connector` fixture's `tools_for_run`, which would leave the second resume with
        # no write tool to call and let the test pass without exercising the replay guard at all.
        armed = {"kill": True}

        def _die_once(*args: Any, **kwargs: Any) -> Any:
            if armed["kill"]:
                armed["kill"] = False
                raise WorkerKilled("worker killed")
            return build_function_response_content(*args, **kwargs)

        monkeypatch.setattr(
            "relay_core.agent.nodes.approval_gate.build_function_response_content", _die_once
        )
        with pytest.raises(WorkerKilled):
            await resume_agent_once(
                workspace_id,
                uuid.UUID(run_id),
                decision,
                session=db_session,
                redis=redis_client,
                settings=test_settings,
                checkpointer=checkpointer,
                gateway=gateway,
            )

        # The side effect happened, and the record of it outlived the kill.
        assert len(email_connector.sent) == 1
        calls = await ToolCallRepository(db_session).list_for_run(workspace_id, uuid.UUID(run_id))
        assert [c.status for c in calls] == ["succeeded"]

        await resume_agent_once(
            workspace_id,
            uuid.UUID(run_id),
            decision,
            session=db_session,
            redis=redis_client,
            settings=test_settings,
            checkpointer=checkpointer,
            gateway=gateway,
        )

        assert len(email_connector.sent) == 1, email_connector.sent
        run_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers
        )
        assert run_resp.json()["status"] == "completed", run_resp.json()
    finally:
        app.dependency_overrides.pop(get_run_dispatcher, None)


async def test_the_watchdog_expires_an_approval_nobody_decided(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    email_connector: RecordingEmailConnector,
) -> None:
    """docs/system-design.md section 13.4: an undecided approval expires rather than pinning the
    run open forever. The sweep has to close out all three records — the approval, the proposed
    calls that will now never run, and the run itself, which can never be resumed."""
    headers, workspace_id, conversation_id = await _register(client, "approve-expiry@example.com")
    _install_dispatchers(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=_scripted_gateway(db_session, redis_client, test_settings, responses=_script()),
    )
    try:
        run_id, approval_json = await _send_and_park(
            client, headers, workspace_id, conversation_id
        )
        approvals = ApprovalRepository(db_session)
        approval = await approvals.get(workspace_id, uuid.UUID(approval_json["id"]))
        assert approval is not None
        approval.expires_at = datetime.now(UTC) - timedelta(hours=1)
        await db_session.flush()

        swept = await expire_stale_approvals(db_session, EventPublisher(redis_client))
        assert swept == 1

        refreshed = await approvals.get(workspace_id, approval.id)
        assert refreshed is not None
        assert refreshed.status == "expired"

        calls = await ToolCallRepository(db_session).list_for_run(workspace_id, uuid.UUID(run_id))
        assert [c.status for c in calls] == ["skipped"]
        assert email_connector.sent == []

        run_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers
        )
        assert run_resp.json()["status"] == "expired", run_resp.json()
    finally:
        app.dependency_overrides.pop(get_run_dispatcher, None)
        app.dependency_overrides.pop(get_resume_dispatcher, None)


async def test_a_second_decision_is_refused_so_the_write_cannot_replay(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    email_connector: RecordingEmailConnector,
) -> None:
    """The double-submit guard (`ApprovalRepository.decide`). Without it a retried request would
    resume the same parked checkpoint twice and send the email twice."""
    headers, workspace_id, conversation_id = await _register(client, "approve-twice@example.com")
    _install_dispatchers(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=_scripted_gateway(db_session, redis_client, test_settings, responses=_script()),
    )
    try:
        _run_id, approval = await _send_and_park(client, headers, workspace_id, conversation_id)
        url = f"/api/v1/workspaces/{workspace_id}/approvals/{approval['id']}/decision"

        first = await client.post(url, json={"action": "approve"}, headers=headers)
        assert first.status_code == 200, first.text

        second = await client.post(url, json={"action": "approve"}, headers=headers)
        assert second.status_code == 409, second.text

        assert len(email_connector.sent) == 1
    finally:
        app.dependency_overrides.pop(get_run_dispatcher, None)
        app.dependency_overrides.pop(get_resume_dispatcher, None)
