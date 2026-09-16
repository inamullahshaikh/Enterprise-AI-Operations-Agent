import uuid

import pytest

from relay_core.agent.nodes.check_capabilities import CheckCapabilities
from relay_core.agent.state import AgentState, Plan, PlanStep

pytestmark = pytest.mark.asyncio


def _state(plan: Plan, available: list[str] | None = None) -> AgentState:
    return AgentState(
        workspace_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        trigger_message_id=uuid.uuid4(),
        user_message="does not matter here",
        available_capabilities=available or [],
        plan=plan,
    )


async def test_reports_every_unavailable_capability() -> None:
    plan = Plan(
        objective="o",
        steps=[
            PlanStep(
                id="s1",
                goal="Pull expiring subscriptions",
                required_capabilities=["subscription.read"],
                expected_output="a list",
            ),
            PlanStep(
                id="s2",
                goal="Draft follow-up emails",
                required_capabilities=["email.draft"],
                expected_output="drafts",
            ),
        ],
    )
    node = CheckCapabilities(deps=None)  # type: ignore[arg-type]

    result = await node(_state(plan))

    capabilities = {m["capability"] for m in result["missing"]}
    assert capabilities == {"subscription.read", "email.draft"}
    assert all(m["type"] == "missing_capability" for m in result["missing"])


async def test_no_gap_when_every_capability_is_available() -> None:
    plan = Plan(
        objective="o",
        steps=[
            PlanStep(
                id="s1",
                goal="Search docs",
                required_capabilities=["knowledge.search"],
                expected_output="x",
            )
        ],
    )
    node = CheckCapabilities(deps=None)  # type: ignore[arg-type]

    result = await node(_state(plan, available=["knowledge.search"]))

    assert result["missing"] == []


async def test_needs_clarification_short_circuits_to_a_single_entry() -> None:
    plan = Plan(objective="o", steps=[], needs_clarification="Which quarter do you mean?")
    node = CheckCapabilities(deps=None)  # type: ignore[arg-type]

    result = await node(_state(plan))

    assert result["missing"] == [
        {"type": "clarification", "question": "Which quarter do you mean?"}
    ]
