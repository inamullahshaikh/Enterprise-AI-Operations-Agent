"""A fake connector with one read tool and one write tool, bound into every run by patching
`ToolRegistry.tools_for_run`. Shared by the Phase 8 injection and data-flow tests, which need to
control exactly what a read returns and whether its source counts as untrusted.

Same approach as `test_approval_flow.py`'s recording connector: everything from `execute_step`
to the approval row is real, only the tool at the end is a stand-in.
"""

from typing import Any

import pytest
from google.genai import types
from pydantic import BaseModel

from relay_core.agent.nodes.guard_input import GuardVerdict
from relay_core.agent.nodes.route import RouteVerdict
from relay_core.agent.state import Plan, PlanStep
from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    Risk,
    ToolResult,
    ToolSpec,
)
from relay_core.tools.registry import BoundTool, BoundToolSet, ToolRegistry
from tests.integration.scripted_model import text_response

READ = "fake__read_inbox"
WRITE = "fake__create_draft"


class _NoConfig(BaseModel):
    pass


class FakeConnector(Connector):
    key = "fake"
    display_name = "Fake"
    auth_type = AuthType.NONE
    config_model = _NoConfig
    secrets_model = _NoConfig

    def __init__(self) -> None:
        self.inbox: Any = {"messages": []}
        self.drafts: list[dict[str, Any]] = []

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        schema = {"type": "object", "properties": {"to": {"type": "string"}}}
        return [
            ToolSpec(name="read_inbox", description="Read mail.", input_schema=schema),
            ToolSpec(
                name="create_draft",
                description="Draft mail.",
                input_schema=schema,
                risk=Risk.WRITE,
                idempotent=False,
            ),
        ]

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        if tool_name == "read_inbox":
            return ToolResult(ok=True, content=self.inbox)
        self.drafts.append(args)
        return ToolResult(ok=True, content={"draft_id": f"d{len(self.drafts)}"})

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        return True, "ok"


@pytest.fixture
def fake_connector(monkeypatch: pytest.MonkeyPatch) -> FakeConnector:
    connector = FakeConnector()

    async def _capabilities(*_args: Any, **_kwargs: Any) -> list[str]:
        return ["email.read", "email.draft"]

    monkeypatch.setattr(
        "relay_core.agent.nodes.load_context.resolve_available_capabilities", _capabilities
    )

    async def _tools_for_run(self: ToolRegistry, **kw: Any) -> BoundToolSet:
        ctx = ExecutionContext(
            workspace_id=kw["workspace_id"],
            user_id=kw["user_id"],
            run_id=kw["run_id"],
            conversation_id=kw["conversation_id"],
            installation_id="fake",
        )
        return BoundToolSet(
            [
                BoundTool(
                    llm_name=f"fake__{spec.name}",
                    installation_id=None,
                    connector=connector,
                    ctx=ctx,
                    spec=spec,
                )
                for spec in await connector.list_tools(ctx)
            ]
        )

    monkeypatch.setattr(ToolRegistry, "tools_for_run", _tools_for_run)
    return connector


def function_call(name: str, args: dict[str, Any]) -> types.GenerateContentResponse:
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
            prompt_token_count=5, candidates_token_count=5
        ),
    )


def preamble(goal: str) -> list[types.GenerateContentResponse]:
    plan = Plan(
        objective=goal,
        steps=[
            PlanStep(
                id="s1",
                goal=goal,
                required_capabilities=["email.read", "email.draft"],
                expected_output="done",
            )
        ],
    )
    return [
        text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
        text_response(RouteVerdict(route="task").model_dump_json()),
        text_response(plan.model_dump_json()),
    ]
