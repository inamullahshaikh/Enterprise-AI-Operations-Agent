"""Memory extraction after a run (Phase 7 D1, docs/system-design.md section 12.2).

Every test here scripts the extractor to propose something it should *not* be allowed to store,
or something it should be made to merge, because the prompt is not the guarantee — the filters
in `relay_core.memory.extract` are. A test that only proved the happy path would still pass with
every one of them deleted.
"""

import json
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.llm_calls import UsageTotals
from relay_core.db.repositories.memories import MemoryRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.db.repositories.policies import WorkspacePolicyRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository, WorkspaceRepository
from relay_core.memory.extract import ExtractedMemory, extract_memories
from tests.integration.scripted_model import scripted_gateway, text_response

pytestmark = pytest.mark.asyncio


class _Fixture:
    def __init__(self, workspace_id: uuid.UUID, user_id: uuid.UUID, run_id: uuid.UUID) -> None:
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.run_id = run_id


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


async def _completed_run(session: AsyncSession, *, role: str = "owner") -> _Fixture:
    """One finished run with a user message and an answer — the whole input the extractor
    sees."""
    user = await UserRepository(session).create(
        email=f"mem-{uuid.uuid4().hex[:8]}@example.com", full_name="Dev", password_hash="x"
    )
    await session.flush()
    workspace = await WorkspaceRepository(session).create(
        name="Memory Co", slug=f"mem-{uuid.uuid4().hex[:8]}", created_by=user.id
    )
    await session.flush()
    members = WorkspaceMemberRepository(session)
    membership = await members.get(workspace.id, user.id)
    if membership is None:
        await members.add(workspace_id=workspace.id, user_id=user.id, role=role)
    else:
        membership.role = role
    await session.flush()

    conversation = await ConversationRepository(session).create(
        workspace_id=workspace.id, user_id=user.id
    )
    await session.flush()
    messages = MessageRepository(session)
    trigger = await messages.create(
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        role="user",
        content="Draft the renewal note. And always write my emails in a formal tone.",
    )
    answer = await messages.create(
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        role="assistant",
        content="Drafted the renewal note formally.",
    )
    run = await AgentRunRepository(session).create(
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        user_id=user.id,
        trigger_message_id=trigger.id,
    )
    await session.flush()
    await AgentRunRepository(session).mark_running(workspace.id, run.id)
    await AgentRunRepository(session).mark_completed(
        workspace.id,
        run.id,
        final_message_id=answer.id,
        route="task",
        usage=UsageTotals(
            llm_calls=0,
            input_tokens=0,
            output_tokens=0,
            thought_tokens=0,
            cost_usd=Decimal(0),
        ),
        tool_calls=0,
        capability_snapshot=None,
    )
    await session.flush()
    return _Fixture(workspace.id, user.id, run.id)


def _extraction(*memories: ExtractedMemory) -> object:
    return text_response(json.dumps({"memories": [m.model_dump() for m in memories]}))


async def _extract(
    fixture: _Fixture,
    db_session: AsyncSession,
    redis_client,
    test_settings: Settings,
    *proposals: ExtractedMemory,
    vectors: list[list[float]] | None = None,
):
    gateway, models = scripted_gateway(
        db_session,
        redis_client,
        test_settings,
        responses=[_extraction(*proposals)],
        stream_texts=[],
    )
    supplied = vectors or [[1.0] + [0.0] * 767 for _ in proposals]
    gateway.embed = _embeddings(supplied)  # type: ignore[method-assign]
    written = await extract_memories(
        workspace_id=fixture.workspace_id,
        run_id=fixture.run_id,
        session=db_session,
        gateway=gateway,
        settings=test_settings,
    )
    return written, models


def _embeddings(vectors: list[list[float]]):
    """Gemini is unreachable in integration tests, so the embedding call is the one seam these
    tests fake directly — the filters under test all run either side of it."""

    async def _embed(texts: list[str], *, task: str, settings: Settings) -> list[list[float]]:
        return vectors[: len(texts)]

    return _embed


async def test_a_stated_preference_becomes_one_memory_pointing_at_its_run(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    fixture = await _completed_run(db_session)

    written, _ = await _extract(
        fixture,
        db_session,
        redis_client,
        test_settings,
        ExtractedMemory(
            content="Wants email drafts written in a formal tone",
            kind="preference",
            scope="user",
            confidence=0.9,
        ),
    )

    assert len(written) == 1
    assert written[0].kind == "preference"
    assert written[0].scope == "user"
    assert written[0].source_run_id == fixture.run_id
    assert written[0].embedding_model == test_settings.embedding_model


async def test_a_low_confidence_extraction_is_dropped(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    fixture = await _completed_run(db_session)

    written, _ = await _extract(
        fixture,
        db_session,
        redis_client,
        test_settings,
        ExtractedMemory(
            content="Might prefer bullet points", kind="preference", scope="user", confidence=0.5
        ),
    )

    assert written == []
    rows = await MemoryRepository(db_session).list_visible_to(
        fixture.workspace_id, fixture.user_id
    )
    assert rows == []


async def test_a_near_duplicate_updates_the_existing_row_instead_of_adding_one(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    fixture = await _completed_run(db_session)
    memories = MemoryRepository(db_session)
    existing = await memories.create(
        workspace_id=fixture.workspace_id,
        user_id=fixture.user_id,
        scope="user",
        kind="preference",
        content="Prefers formal drafts",
        confidence=0.8,
        embedding=[1.0] + [0.0] * 767,
        embedding_model=test_settings.embedding_model,
    )
    await db_session.flush()

    written, _ = await _extract(
        fixture,
        db_session,
        redis_client,
        test_settings,
        ExtractedMemory(
            content="Wants email drafts written in a formal tone",
            kind="preference",
            scope="user",
            confidence=0.95,
        ),
    )

    assert [m.id for m in written] == [existing.id]
    rows = await memories.list_visible_to(fixture.workspace_id, fixture.user_id)
    assert len(rows) == 1
    assert rows[0].content == "Wants email drafts written in a formal tone"
    assert rows[0].confidence == 0.95


async def test_something_shaped_like_an_api_key_is_never_stored(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    fixture = await _completed_run(db_session)

    written, _ = await _extract(
        fixture,
        db_session,
        redis_client,
        test_settings,
        ExtractedMemory(
            content="Their Stripe key is sk-live-4f8a9c2e7b1d6035aa",
            kind="fact",
            scope="user",
            confidence=0.99,
        ),
        ExtractedMemory(
            content="The database password is hunter2",
            kind="fact",
            scope="user",
            confidence=0.99,
        ),
    )

    assert written == []


async def test_a_members_workspace_scope_proposal_is_stored_as_their_own(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    """Section 12.2's "workspace-scope memories require admin visibility": a member's
    observation is still worth keeping, just not on everybody's behalf."""
    fixture = await _completed_run(db_session, role="member")

    written, _ = await _extract(
        fixture,
        db_session,
        redis_client,
        test_settings,
        ExtractedMemory(
            content="Renewal reviews happen on the first of the month",
            kind="procedure",
            scope="workspace",
            confidence=0.9,
        ),
    )

    assert [m.scope for m in written] == ["user"]
    assert written[0].user_id == fixture.user_id


async def test_memory_disabled_for_the_workspace_stores_nothing_and_calls_no_model(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    fixture = await _completed_run(db_session)
    policy = await WorkspacePolicyRepository(db_session).get(fixture.workspace_id)
    policy.memory_enabled = False
    await db_session.flush()

    written, models = await _extract(
        fixture,
        db_session,
        redis_client,
        test_settings,
        ExtractedMemory(
            content="Wants formal drafts", kind="preference", scope="user", confidence=0.9
        ),
    )

    assert written == []
    assert models.calls == []


async def test_a_failed_run_is_never_extracted_from(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    fixture = await _completed_run(db_session)
    await AgentRunRepository(db_session).mark_failed(
        fixture.workspace_id, fixture.run_id, error_code="internal_error", error_message="boom"
    )
    await db_session.flush()

    written, models = await _extract(
        fixture,
        db_session,
        redis_client,
        test_settings,
        ExtractedMemory(
            content="Wants formal drafts", kind="preference", scope="user", confidence=0.9
        ),
    )

    assert written == []
    assert models.calls == []


async def test_finalize_asks_for_extraction_once_a_run_completes(
    client, db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    """The wiring, not the extractor: `finalize` is what puts a completed run on the memory
    queue (section 8.5), and nothing else in the graph does."""
    from relay_core.agent.nodes.guard_input import GuardVerdict
    from relay_core.agent.nodes.route import RouteVerdict
    from tests.integration.scripted_model import (
        install_dispatcher,
        register_workspace_and_conversation,
        send_and_wait,
    )

    enqueued: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def _record(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
        enqueued.append((workspace_id, run_id))

    gateway, _ = scripted_gateway(
        db_session,
        redis_client,
        test_settings,
        responses=[
            text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
            text_response(RouteVerdict(route="direct").model_dump_json()),
        ],
        stream_texts=["Here you go."],
    )
    gateway.embed = _embeddings([[1.0] + [0.0] * 767])  # type: ignore[method-assign]

    headers, workspace_id, conversation_id = await register_workspace_and_conversation(
        client, f"finalize-{uuid.uuid4().hex[:8]}@example.com"
    )
    teardown = install_dispatcher(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=gateway,
        extract_memories=_record,
    )
    try:
        run = await send_and_wait(client, headers, workspace_id, conversation_id, "Say hello")
    finally:
        teardown()

    assert run["status"] == "completed"
    assert enqueued == [(workspace_id, uuid.UUID(run["id"]))]
