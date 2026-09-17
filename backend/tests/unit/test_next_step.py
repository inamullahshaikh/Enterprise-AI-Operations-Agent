import uuid
from typing import Any

import pytest

from relay_core.agent.deps import AgentDeps
from relay_core.agent.nodes.next_step import NextStep
from relay_core.agent.state import AgentState, Plan, PlanStep

pytestmark = pytest.mark.asyncio


class _RecordingEvents:
    def __init__(self) -> None:
        self.published: list[tuple[Any, ...]] = []

    async def publish(self, run_id: Any, event_type: Any, payload: Any) -> None:
        self.published.append((run_id, event_type, payload))


class _RecordingRuns:
    def __init__(self) -> None:
        self.plans: list[Any] = []

    async def set_plan(self, workspace_id: Any, run_id: Any, plan: Any) -> None:
        self.plans.append(plan)


def _deps(events: _RecordingEvents, runs: _RecordingRuns) -> AgentDeps:
    return AgentDeps(
        gateway=None,  # type: ignore[arg-type]
        events=events,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        conversations=None,  # type: ignore[arg-type]
        messages=None,  # type: ignore[arg-type]
        runs=runs,  # type: ignore[arg-type]
        llm_calls=None,  # type: ignore[arg-type]
        tool_calls=None,  # type: ignore[arg-type]
        tool_definitions=None,  # type: ignore[arg-type]
        attachments=None,  # type: ignore[arg-type]
        documents=None,  # type: ignore[arg-type]
        tool_registry=None,  # type: ignore[arg-type]
        tool_executor=None,  # type: ignore[arg-type]
        approvals=None,  # type: ignore[arg-type]
        policies=None,  # type: ignore[arg-type]
        members=None,  # type: ignore[arg-type]
        memories=None,  # type: ignore[arg-type]
        extract_memories=None,  # type: ignore[arg-type]
    )


def _state(plan: Plan, current_step_id: str | None = None) -> AgentState:
    return AgentState(
        workspace_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        trigger_message_id=uuid.uuid4(),
        user_message="does not matter here",
        plan=plan,
        current_step_id=current_step_id,
    )


async def test_picks_the_first_pending_step_with_no_dependencies() -> None:
    plan = Plan(
        objective="o",
        steps=[
            PlanStep(id="s1", goal="first", expected_output="x"),
            PlanStep(id="s2", goal="second", expected_output="y", depends_on=["s1"]),
        ],
    )
    node = NextStep(_deps(_RecordingEvents(), _RecordingRuns()))

    result = await node(_state(plan))

    assert result["current_step_id"] == "s1"
    assert result["plan"].steps[0].status == "running"


async def test_waits_for_a_dependency_that_is_not_done_yet() -> None:
    plan = Plan(
        objective="o",
        steps=[
            PlanStep(id="s1", goal="first", expected_output="x", status="running"),
            PlanStep(id="s2", goal="second", expected_output="y", depends_on=["s1"]),
        ],
    )
    node = NextStep(_deps(_RecordingEvents(), _RecordingRuns()))

    result = await node(_state(plan))

    assert result["current_step_id"] is None


async def test_picks_a_dependent_step_once_its_dependency_is_done() -> None:
    plan = Plan(
        objective="o",
        steps=[
            PlanStep(id="s1", goal="first", expected_output="x", status="done"),
            PlanStep(id="s2", goal="second", expected_output="y", depends_on=["s1"]),
        ],
    )
    node = NextStep(_deps(_RecordingEvents(), _RecordingRuns()))

    result = await node(_state(plan, current_step_id="s1"))

    assert result["current_step_id"] == "s2"


async def test_cascades_skip_to_a_step_depending_on_a_failed_one() -> None:
    plan = Plan(
        objective="o",
        steps=[
            PlanStep(id="s1", goal="first", expected_output="x", status="failed"),
            PlanStep(id="s2", goal="second", expected_output="y", depends_on=["s1"]),
        ],
    )
    node = NextStep(_deps(_RecordingEvents(), _RecordingRuns()))

    result = await node(_state(plan, current_step_id="s1"))

    assert result["current_step_id"] is None
    assert result["plan"].steps[1].status == "skipped"


async def test_returns_none_and_persists_the_plan_when_everything_is_done() -> None:
    plan = Plan(
        objective="o", steps=[PlanStep(id="s1", goal="first", expected_output="x", status="done")]
    )
    runs = _RecordingRuns()
    node = NextStep(_deps(_RecordingEvents(), runs))

    result = await node(_state(plan, current_step_id="s1"))

    assert result["current_step_id"] is None
    assert len(runs.plans) == 1
    assert runs.plans[0]["steps"][0]["status"] == "done"


async def test_publishes_step_started_and_step_finished_events() -> None:
    plan = Plan(
        objective="o",
        steps=[
            PlanStep(id="s1", goal="first", expected_output="x", status="done"),
            PlanStep(id="s2", goal="second", expected_output="y"),
        ],
    )
    events = _RecordingEvents()
    node = NextStep(_deps(events, _RecordingRuns()))

    await node(_state(plan, current_step_id="s1"))

    event_types = [e[1] for e in events.published]
    assert "step.finished" in event_types
    assert "step.started" in event_types
