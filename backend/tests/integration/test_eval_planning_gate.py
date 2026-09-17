"""The `planning` suite's gate can go red (Phase 7 F1).

A gate that cannot fail is not a gate — the Phase 5 lesson, recorded in
`docs/phase-5-status.md`. `expectations.expect_replan` is scored from the number of `planner`
calls a run made, which is a proxy: it would pass just as happily if `replan` never existed and
something else called the planner twice. So this drives two real runs through the real graph
against a scripted model — one where `validate_step` returns `replan` and one where it returns
`fail` at the same dead end — and scores both against the same expectation. The first must pass
and the second must fail.

The `fail` run is what the suite would see if C1's node were removed: the same objective, the
same unworkable step, and an honest report of the dead end instead of a route around it.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from relay_eval.cases import EvalCase, Expectations
from relay_eval.scoring import score_case
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.agent.nodes.guard_input import GuardVerdict
from relay_core.agent.nodes.route import RouteVerdict
from relay_core.agent.nodes.validate_final import FinalVerdict
from relay_core.agent.nodes.validate_step import StepVerdict
from relay_core.agent.state import Plan, PlanStep
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.llm_calls import LLMCallRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from tests.integration.scripted_model import (
    install_dispatcher,
    register_workspace_and_conversation,
    scripted_gateway,
    send_and_wait,
    text_response,
)

pytestmark = pytest.mark.asyncio

_CASE = EvalCase(
    key="planning_gate_probe",
    suite="planning",
    message="Report on this month's renewals",
    expectations=Expectations(route="task", expect_replan=True),
)


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


def _plan(step_id: str, goal: str) -> Plan:
    return Plan(
        objective="Report on this month's renewals",
        steps=[PlanStep(id=step_id, goal=goal, expected_output="renewal rows")],
    )


def _preamble(plan: Plan) -> list:
    return [
        text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
        text_response(RouteVerdict(route="task").model_dump_json()),
        text_response(plan.model_dump_json()),
    ]


async def _run_and_score(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    *,
    responses: list,
    stream_texts: list[str],
):
    headers, workspace_id, conversation_id = await register_workspace_and_conversation(
        client, f"gate-{uuid.uuid4().hex[:8]}@example.com"
    )
    gateway, _ = scripted_gateway(
        db_session, redis_client, test_settings, responses=responses, stream_texts=stream_texts
    )
    teardown = install_dispatcher(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=gateway,
    )
    try:
        run_json = await send_and_wait(
            client, headers, workspace_id, conversation_id, _CASE.message
        )
    finally:
        teardown()

    run_id = uuid.UUID(run_json["id"])
    agent_run = await AgentRunRepository(db_session).get(workspace_id, run_id)
    assert agent_run is not None
    planner_calls = await LLMCallRepository(db_session).count_for_node(
        workspace_id, run_id, "planner"
    )
    tool_calls = await ToolCallRepository(db_session).list_for_run(workspace_id, run_id)
    return await score_case(
        _CASE,
        agent_run,
        tool_calls,
        test_settings,
        latency_s=0.0,
        final_answer=run_json.get("final_answer"),
        planner_calls=planner_calls,
    )


async def test_a_run_that_replans_passes_the_planning_expectation(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    first = _plan("s1", "Pull renewals from the CRM")
    revised = _plan("s2", "Read renewals from the database")

    result = await _run_and_score(
        client,
        db_session,
        redis_client,
        test_settings,
        responses=[
            *_preamble(first),
            text_response("The CRM is not installed."),
            text_response(
                StepVerdict(status="replan", reason="No CRM in this workspace.").model_dump_json()
            ),
            text_response(revised.model_dump_json()),
            text_response("Three renewals close this month."),
            text_response(StepVerdict(status="pass", reason="Rows returned.").model_dump_json()),
            text_response(FinalVerdict(status="pass", reason="Grounded.").model_dump_json()),
        ],
        stream_texts=["Three renewals close this month."],
    )

    assert result.passed, result.reasons


async def test_the_same_dead_end_without_a_replan_fails_the_planning_expectation(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    """This is the run the suite would see with C1 removed: the step fails, the plan stands, and
    the answer honestly reports the gap. Truthful, and still not what `planning` measures."""
    first = _plan("s1", "Pull renewals from the CRM")

    result = await _run_and_score(
        client,
        db_session,
        redis_client,
        test_settings,
        responses=[
            *_preamble(first),
            text_response("The CRM is not installed."),
            text_response(
                StepVerdict(status="fail", reason="No CRM in this workspace.").model_dump_json()
            ),
            text_response(FinalVerdict(status="pass", reason="Grounded.").model_dump_json()),
        ],
        stream_texts=["I could not reach a CRM, so I have no renewals to report."],
    )

    assert not result.passed
    assert any("expected the plan to be revised" in reason for reason in result.reasons)
