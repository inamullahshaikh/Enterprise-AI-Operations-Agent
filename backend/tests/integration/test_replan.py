"""`replan` (Phase 7 C1): a step that cannot work revises the rest of the plan instead of ending
the run.

Driven through the real HTTP message endpoint against a scripted Gemini client, like the Phase 3
and Phase 5 flow tests: the point is that the graph's own edges route the way they are supposed
to, which a test that called the node directly would never check.

Steps here require no capabilities, so `execute_step` runs them with no tools bound and the
model's text turn becomes the step's `result_summary`. What is under test is the routing, not
tool execution — `test_execute_step_flow.py` covers that.
"""

import json
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.agent.nodes.guard_input import GuardVerdict
from relay_core.agent.nodes.route import RouteVerdict
from relay_core.agent.nodes.validate_final import FinalVerdict
from relay_core.agent.nodes.validate_step import StepVerdict
from relay_core.agent.state import Plan, PlanStep
from relay_core.events.types import PLAN_UPDATED
from tests.integration.scripted_model import (
    install_dispatcher,
    published_events,
    register_workspace_and_conversation,
    scripted_gateway,
    send_and_wait,
    text_response,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


def _plan(*goals: str) -> Plan:
    return Plan(
        objective="Report on this month's renewals",
        steps=[
            PlanStep(id=f"s{i + 1}", goal=goal, expected_output=f"result of {goal}")
            for i, goal in enumerate(goals)
        ],
    )


def _preamble() -> list:
    return [
        text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
        text_response(RouteVerdict(route="task").model_dump_json()),
    ]


def _verdict(status: str, reason: str) -> object:
    return text_response(StepVerdict(status=status, reason=reason).model_dump_json())


def _grounded() -> object:
    """`validate_final` runs on every synthesized answer (C2), so every run that reaches
    `synthesize` needs one more scripted verdict than it did before Phase 7."""
    return text_response(FinalVerdict(status="pass", reason="Grounded.").model_dump_json())


async def _run(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    email: str,
    *,
    responses: list,
    stream_texts: list[str],
) -> tuple[dict, object]:
    headers, workspace_id, conversation_id = await register_workspace_and_conversation(
        client, email
    )
    gateway, models = scripted_gateway(
        db_session,
        redis_client,
        test_settings,
        responses=responses,
        stream_texts=stream_texts,
    )
    teardown = install_dispatcher(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=gateway,
    )
    try:
        run = await send_and_wait(
            client, headers, workspace_id, conversation_id, "How are renewals looking?"
        )
    finally:
        teardown()
    return run, models


async def test_a_replan_verdict_produces_a_new_plan_the_run_finishes_on(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
) -> None:
    first = _plan("Pull renewals from the CRM")
    revised = Plan(
        objective=first.objective,
        steps=[PlanStep(id="s2", goal="Read renewals from the database", expected_output="rows")],
    )
    responses = [
        *_preamble(),
        text_response(first.model_dump_json()),
        text_response("The CRM connector is not installed, so I could not pull anything."),
        _verdict("replan", "The CRM is unavailable; another source could still answer this."),
        text_response(revised.model_dump_json()),
        text_response("Three subscriptions renew this month."),
        _verdict("pass", "The step produced the rows."),
        _grounded(),
    ]

    run, models = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "replan-happy@example.com",
        responses=responses,
        stream_texts=["Three subscriptions renew this month."],
    )

    assert run["status"] == "completed", run
    steps = {s["id"]: s for s in run["plan"]["steps"]}
    assert "s1" not in steps, "the failed step is replaced, not carried along"
    assert steps["s2"]["status"] == "done"
    assert models.responses == [], "every scripted response was used"

    events = await published_events(redis_client, run["id"])
    updated = [json.loads(e["data"]) for e in events if e["type"] == PLAN_UPDATED]
    assert len(updated) == 1
    assert [s["id"] for s in updated[0]["steps"]] == ["s2"]
    assert "CRM is unavailable" in updated[0]["reason"]


async def test_completed_steps_and_their_results_survive_replanning(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
) -> None:
    first = _plan("Count active accounts", "Email each one")
    # The revision re-proposes the finished step (models do) *and* renames it. Neither may
    # touch what s1 already produced.
    revised = Plan(
        objective=first.objective,
        steps=[
            PlanStep(id="s1", goal="Count accounts again", expected_output="a number"),
            PlanStep(id="s3", goal="Draft one summary instead", expected_output="a summary"),
        ],
    )
    responses = [
        *_preamble(),
        text_response(first.model_dump_json()),
        text_response("There are 42 active accounts."),
        _verdict("pass", "Counted."),
        text_response("Emailing each one is not possible without the mail connector."),
        _verdict("replan", "Per-account email is unavailable; summarize instead."),
        text_response(revised.model_dump_json()),
        text_response("Summary drafted for all 42 accounts."),
        _verdict("pass", "Summary written."),
        _grounded(),
    ]

    run, _ = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "replan-preserve@example.com",
        responses=responses,
        stream_texts=["42 accounts, one summary."],
    )

    steps = {s["id"]: s for s in run["plan"]["steps"]}
    assert steps["s1"]["status"] == "done"
    assert steps["s1"]["goal"] == "Count active accounts", "a finished step is kept verbatim"
    assert steps["s1"]["result_summary"] == "There are 42 active accounts."
    assert steps["s3"]["status"] == "done"
    assert "s2" not in steps


async def test_the_third_replan_never_happens(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
) -> None:
    """Two revisions, then the run answers with what it has. A truthful partial answer beats a
    loop that spends the whole budget rediscovering the same dead end."""
    plan_json = [text_response(_plan(f"Try approach {i}").model_dump_json()) for i in range(1, 4)]
    responses = [
        *_preamble(),
        plan_json[0],
        text_response("Approach 1 did not work."),
        _verdict("replan", "Try another way."),
        plan_json[1],
        text_response("Approach 2 did not work either."),
        _verdict("replan", "Try another way."),
        plan_json[2],
        text_response("Approach 3 did not work either."),
        _verdict("replan", "Try another way."),
        _grounded(),
    ]

    run, models = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "replan-bound@example.com",
        responses=responses,
        stream_texts=["I could not find a way to answer this."],
    )

    assert run["status"] == "completed", run
    # The third `replan` verdict degrades to `fail`: no fourth plan was ever requested.
    assert models.responses == []
    assert run["plan"]["steps"][-1]["status"] == "failed"
    assert run["plan"]["steps"][-1]["result_summary"] == "Try another way."


async def test_a_revision_needing_a_missing_capability_asks_instead_of_failing(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
) -> None:
    """`replan` edges to `check_capabilities`, so a revised plan that needs something this
    workspace doesn't have lands on the existing missing-capability card."""
    first = _plan("Summarize the renewals")
    revised = Plan(
        objective=first.objective,
        steps=[
            PlanStep(
                id="s2",
                goal="Query the billing database",
                required_capabilities=["subscription.read"],
                expected_output="rows",
            )
        ],
    )
    responses = [
        *_preamble(),
        text_response(first.model_dump_json()),
        text_response("I have nothing to summarize without the data."),
        _verdict("replan", "The data has to come from somewhere first."),
        text_response(revised.model_dump_json()),
    ]

    run, models = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "replan-missing@example.com",
        responses=responses,
        stream_texts=[],
    )

    assert run["status"] == "awaiting_input", run
    assert models.responses == [], "the run stopped at ask_missing, before synthesizing"
    assert models.calls.count("stream") == 0


async def test_an_unusable_revision_leaves_the_run_with_the_plan_it_had(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
) -> None:
    """A parse failure is a failure, not a retry: the step stays failed and `synthesize`
    explains the gap, exactly as it did before `replan` existed."""
    responses = [
        *_preamble(),
        text_response(_plan("Do the thing").model_dump_json()),
        text_response("The thing could not be done."),
        _verdict("replan", "Perhaps another way."),
        text_response("not json at all"),
        _grounded(),
    ]

    run, _ = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "replan-garbage@example.com",
        responses=responses,
        stream_texts=["I could not complete that."],
    )

    assert run["status"] == "completed", run
    assert run["plan"]["steps"][0]["status"] == "failed"
    assert uuid.UUID(run["id"])
