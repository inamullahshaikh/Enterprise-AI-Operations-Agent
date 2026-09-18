"""Phase 8 B1: a run that spends its `workspace_policies.run_budget` stops early and still
answers, saying what it gathered (docs/system-design.md section 19.2).

Driven through the real message endpoint against a scripted model, like `test_replan.py`. The
budget is set through `PATCH /workspaces/{ws}/policy`, which is what `load_context` reads.
"""

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
from relay_core.events.types import BUDGET_EXCEEDED
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


_PLAN = Plan(
    objective="Two lookups",
    steps=[
        PlanStep(id="s1", goal="Count the renewals", expected_output="a number"),
        PlanStep(id="s2", goal="List the churned accounts", expected_output="names"),
    ],
)


def _preamble() -> list:
    return [
        text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
        text_response(RouteVerdict(route="task").model_dump_json()),
        text_response(_PLAN.model_dump_json()),
    ]


_GROUNDED = text_response(FinalVerdict(status="pass", reason="ok").model_dump_json())


async def _run(client, db_session, redis_client, test_settings, email, budget, responses):
    headers, workspace_id, conversation_id = await register_workspace_and_conversation(
        client, email
    )
    resp = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/policy", json={"run_budget": budget}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    gateway, models = scripted_gateway(
        db_session,
        redis_client,
        test_settings,
        responses=responses,
        stream_texts=["Stopped early: renewals counted, churn not checked."],
    )
    teardown = install_dispatcher(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=gateway,
    )
    try:
        run = await send_and_wait(client, headers, workspace_id, conversation_id, "Two lookups")
    finally:
        teardown()
    return run, models


async def test_llm_call_budget_stops_the_run_with_a_partial_answer(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    # guard + route + plan + s1's executor turn = 4, so s2 finds the budget spent.
    responses = [
        *_preamble(),
        text_response("Three renewals."),
        text_response(StepVerdict(status="pass", reason="ok").model_dump_json()),
        _GROUNDED,
    ]
    run, models = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "budget-llm@example.com",
        {"max_llm_calls": 4},
        responses,
    )

    assert run["status"] == "completed", run
    statuses = {s["id"]: (s["status"], s["result_summary"]) for s in run["plan"]["steps"]}
    assert statuses["s1"] == ("done", "Three renewals.")
    assert statuses["s2"][0] == "failed" and "LLM-call budget" in statuses["s2"][1]
    assert "stopped early" in models.systems[models.calls.index("stream")]
    assert models.responses == []

    events = await published_events(redis_client, run["id"])
    assert sum(1 for e in events if e.get("type") == BUDGET_EXCEEDED) == 1


async def test_cost_budget_below_one_call_stops_before_the_first_step(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    run, models = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "budget-cost@example.com",
        {"max_cost_usd": 0.0000001},
        [*_preamble(), _GROUNDED],
    )

    assert run["status"] == "completed", run
    statuses = {s["id"]: s["status"] for s in run["plan"]["steps"]}
    assert statuses == {"s1": "failed", "s2": "skipped"}
    assert models.responses == []  # no executor turn was ever made


async def test_a_run_inside_its_budget_publishes_no_budget_event(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    pass_ = text_response(StepVerdict(status="pass", reason="ok").model_dump_json())
    responses = [
        *_preamble(),
        text_response("Three renewals."),
        pass_,
        text_response("Two churned."),
        pass_,
        _GROUNDED,
    ]
    run, models = await _run(
        client, db_session, redis_client, test_settings, "budget-ok@example.com", {}, responses
    )

    assert run["status"] == "completed", run
    assert all(s["status"] == "done" for s in run["plan"]["steps"])
    assert "stopped early" not in models.systems[models.calls.index("stream")]
    events = await published_events(redis_client, run["id"])
    assert not any(e.get("type") == BUDGET_EXCEEDED for e in events)
