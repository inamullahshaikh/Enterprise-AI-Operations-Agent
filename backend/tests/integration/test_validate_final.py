"""`validate_final` (Phase 7 C2): the groundedness check between a synthesized draft and the
user.

The assertion that matters most is the negative one — that no `token` event ever carried text
from a draft the validator rejected. That is the whole reason `synthesize` stopped streaming
its first pass (ADR-0013 decision 5), and it is invisible to a test that only reads the final
message.
"""

import json

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
from relay_core.events.types import TOKEN
from tests.integration.scripted_model import (
    install_dispatcher,
    published_events,
    register_workspace_and_conversation,
    scripted_gateway,
    send_and_wait,
    text_response,
)

pytestmark = pytest.mark.asyncio

_GROUNDED_DRAFT = "Acme Robotics renews this month."
_UNGROUNDED_DRAFT = "Acme Robotics renews this month for $9,900,000."


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


def _preamble() -> list:
    plan = Plan(
        objective="Check this month's renewals",
        steps=[PlanStep(id="s1", goal="Look up renewals", expected_output="account names")],
    )
    return [
        text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
        text_response(RouteVerdict(route="task").model_dump_json()),
        text_response(plan.model_dump_json()),
        text_response("Acme Robotics renews this month."),
        text_response(StepVerdict(status="pass", reason="Found it.").model_dump_json()),
    ]


def _final(status: str, *claims: str) -> object:
    return text_response(
        FinalVerdict(
            status=status, unsupported_claims=list(claims), reason="Checked."
        ).model_dump_json()
    )


async def _run(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
    email: str,
    *,
    responses: list,
    stream_texts: list[str],
) -> tuple[dict, object, list[dict]]:
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
            client, headers, workspace_id, conversation_id, "Which accounts renew this month?"
        )
        messages = await client.get(
            f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
            headers=headers,
        )
    finally:
        teardown()
    return run, models, messages.json()


def _streamed(events: list[dict]) -> str:
    return "".join(json.loads(e["data"])["delta"] for e in events if e["type"] == TOKEN)


async def test_an_ungrounded_draft_is_revised_once_and_only_the_revision_is_seen(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
) -> None:
    run, models, messages = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "validate-final-revise@example.com",
        responses=[*_preamble(), _final("revise", "$9,900,000")],
        stream_texts=[_UNGROUNDED_DRAFT, _GROUNDED_DRAFT],
    )

    assert run["status"] == "completed", run
    assert messages[-1]["content"] == _GROUNDED_DRAFT, "the revision is what is persisted"

    events = await published_events(redis_client, run["id"])
    streamed = _streamed(events)
    assert streamed == _GROUNDED_DRAFT
    assert "9,900,000" not in streamed, "no token event carries text from the rejected draft"
    # Two synthesize passes, and the revision is the second one.
    assert models.calls.count("stream") == 2
    assert models.stream_texts == []


async def test_a_grounded_draft_passes_with_one_validator_call_and_no_revision(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
) -> None:
    run, models, messages = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "validate-final-pass@example.com",
        responses=[*_preamble(), _final("pass")],
        stream_texts=[_GROUNDED_DRAFT],
    )

    assert messages[-1]["content"] == _GROUNDED_DRAFT
    assert models.calls.count("stream") == 1
    assert models.responses == [], "the final verdict was the last call, and there was only one"
    events = await published_events(redis_client, run["id"])
    assert _streamed(events) == _GROUNDED_DRAFT
    # The draft is streamed in the pieces it arrived in, not republished as one blob.
    assert len([e for e in events if e["type"] == TOKEN]) == 2
    assert run["status"] == "completed"


async def test_a_second_revise_verdict_still_finalizes(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
) -> None:
    """The check degrades to a no-op rather than becoming a new way for a run to end with
    nothing. Past the one allowed revision the draft ships, so the second verdict is never even
    requested — the scripted script would fail here if it were."""
    run, models, messages = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "validate-final-twice@example.com",
        responses=[*_preamble(), _final("revise", "$9,900,000")],
        stream_texts=[_UNGROUNDED_DRAFT, "Acme Robotics renews this month for $9,900,001."],
    )

    assert run["status"] == "completed", run
    assert messages[-1]["content"].endswith("$9,900,001.")
    assert models.responses == []


async def test_a_direct_answer_is_never_validated(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    test_settings,
) -> None:
    """There are no tool outputs on the `direct` path to be grounded in, and `direct_answer`
    keeps streaming as it always has."""
    run, models, messages = await _run(
        client,
        db_session,
        redis_client,
        test_settings,
        "validate-final-direct@example.com",
        responses=[
            text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
            text_response(RouteVerdict(route="direct").model_dump_json()),
        ],
        stream_texts=["Relay is an operations agent."],
    )

    assert run["route"] == "direct"
    assert messages[-1]["content"] == "Relay is an operations agent."
    assert models.responses == [], "no validator call was made"
    events = await published_events(redis_client, run["id"])
    assert _streamed(events) == "Relay is an operations agent."
