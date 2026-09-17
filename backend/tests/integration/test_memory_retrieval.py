"""Memory retrieval in `load_context` (Phase 7 D2, docs/system-design.md section 12.3).

D1 proves memories get stored; this proves storing them changes anything. The assertion that
matters is what reaches the *prompt*: a memory in the database that never appears in a system
instruction is a row nobody reads, so these tests inspect the system prompt the scripted model
was called with rather than the state field it came from.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.agent.nodes.guard_input import GuardVerdict
from relay_core.agent.nodes.route import RouteVerdict
from relay_core.config import Settings
from relay_core.db.repositories.memories import MemoryRepository
from relay_core.db.repositories.policies import WorkspacePolicyRepository
from relay_core.db.repositories.users import UserRepository
from tests.integration.scripted_model import (
    install_dispatcher,
    register_workspace_and_conversation,
    scripted_gateway,
    send_and_wait,
    text_response,
)

pytestmark = pytest.mark.asyncio

_TONE = [1.0] + [0.0] * 767
_UNRELATED = [0.0] * 767 + [1.0]
_MEMORY = "Wants email drafts written in a formal tone"


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


def _direct_answer_script() -> list:
    return [
        text_response(GuardVerdict(verdict="allow", reason="").model_dump_json()),
        text_response(RouteVerdict(route="direct").model_dump_json()),
    ]


def _embeddings(vector: list[float] | None):
    """`None` means the embedding call fails — the one degradation `load_context` has to survive."""

    async def _embed(texts: list[str], *, task: str, settings: Settings) -> list[list[float]]:
        if vector is None:
            raise RuntimeError("embedding backend unavailable")
        return [vector for _ in texts]

    return _embed


async def _run_with(
    client: AsyncClient,
    db_session: AsyncSession,
    redis_client,
    test_settings: Settings,
    *,
    query_vector: list[float] | None,
    message: str = "Draft a note to Acme",
    seed=None,
):
    gateway, models = scripted_gateway(
        db_session,
        redis_client,
        test_settings,
        responses=_direct_answer_script(),
        stream_texts=["Here is the note."],
    )
    gateway.embed = _embeddings(query_vector)  # type: ignore[method-assign]

    headers, workspace_id, conversation_id = await register_workspace_and_conversation(
        client, f"mem-{uuid.uuid4().hex[:8]}@example.com"
    )
    if seed is not None:
        await seed(workspace_id, headers)

    teardown = install_dispatcher(
        db_session=db_session,
        redis_client=redis_client,
        test_settings=test_settings,
        gateway=gateway,
    )
    try:
        await send_and_wait(client, headers, workspace_id, conversation_id, message)
    finally:
        teardown()
    return models, workspace_id, headers


def _system_prompts(models) -> list[str]:
    return [str(system) for system in models.systems]


async def _me(client: AsyncClient, headers: dict) -> uuid.UUID:
    resp = await client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["id"])


async def test_a_relevant_memory_reaches_the_prompt(
    client: AsyncClient, db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    async def seed(workspace_id: uuid.UUID, headers: dict) -> None:
        await MemoryRepository(db_session).create(
            workspace_id=workspace_id,
            user_id=await _me(client, headers),
            scope="user",
            kind="preference",
            content=_MEMORY,
            confidence=0.9,
            embedding=_TONE,
            embedding_model=test_settings.embedding_model,
        )
        await db_session.flush()

    models, _, _ = await _run_with(
        client, db_session, redis_client, test_settings, query_vector=_TONE, seed=seed
    )

    assert any(_MEMORY in prompt for prompt in _system_prompts(models))


async def test_an_unrelated_memory_stays_out_of_the_prompt(
    client: AsyncClient, db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    async def seed(workspace_id: uuid.UUID, headers: dict) -> None:
        await MemoryRepository(db_session).create(
            workspace_id=workspace_id,
            user_id=await _me(client, headers),
            scope="user",
            kind="preference",
            content=_MEMORY,
            confidence=0.9,
            embedding=_TONE,
            embedding_model=test_settings.embedding_model,
        )
        await db_session.flush()

    models, _, _ = await _run_with(
        client, db_session, redis_client, test_settings, query_vector=_UNRELATED, seed=seed
    )

    assert not any(_MEMORY in prompt for prompt in _system_prompts(models))


async def test_a_used_memory_has_its_counter_incremented(
    client: AsyncClient, db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    holder: dict[str, uuid.UUID] = {}

    async def seed(workspace_id: uuid.UUID, headers: dict) -> None:
        memory = await MemoryRepository(db_session).create(
            workspace_id=workspace_id,
            user_id=await _me(client, headers),
            scope="user",
            kind="preference",
            content=_MEMORY,
            confidence=0.9,
            embedding=_TONE,
            embedding_model=test_settings.embedding_model,
        )
        await db_session.flush()
        holder["id"] = memory.id
        holder["workspace_id"] = workspace_id

    await _run_with(client, db_session, redis_client, test_settings, query_vector=_TONE, seed=seed)

    memory = await MemoryRepository(db_session).get(holder["workspace_id"], holder["id"])
    assert memory is not None
    assert memory.use_count == 1
    assert memory.last_used_at is not None


async def test_an_embedding_failure_leaves_the_run_working(
    client: AsyncClient, db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    async def seed(workspace_id: uuid.UUID, headers: dict) -> None:
        await MemoryRepository(db_session).create(
            workspace_id=workspace_id,
            user_id=await _me(client, headers),
            scope="user",
            kind="preference",
            content=_MEMORY,
            confidence=0.9,
            embedding=_TONE,
            embedding_model=test_settings.embedding_model,
        )
        await db_session.flush()

    models, _, _ = await _run_with(
        client, db_session, redis_client, test_settings, query_vector=None, seed=seed
    )

    assert "stream" in models.calls  # the answer was still produced
    assert not any(_MEMORY in prompt for prompt in _system_prompts(models))


async def test_another_users_memory_never_appears(
    client: AsyncClient, db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    other_secret = "Prefers terse bullet points"

    async def seed(workspace_id: uuid.UUID, headers: dict) -> None:
        stranger = await UserRepository(db_session).create(
            email=f"other-{uuid.uuid4().hex[:8]}@example.com", full_name="Other", password_hash="x"
        )
        await db_session.flush()
        await MemoryRepository(db_session).create(
            workspace_id=workspace_id,
            user_id=stranger.id,
            scope="user",
            kind="preference",
            content=other_secret,
            confidence=0.9,
            embedding=_TONE,
            embedding_model=test_settings.embedding_model,
        )
        await db_session.flush()

    models, _, _ = await _run_with(
        client, db_session, redis_client, test_settings, query_vector=_TONE, seed=seed
    )

    assert not any(other_secret in prompt for prompt in _system_prompts(models))


async def test_memory_disabled_for_the_workspace_skips_retrieval_entirely(
    client: AsyncClient, db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    async def seed(workspace_id: uuid.UUID, headers: dict) -> None:
        policy = await WorkspacePolicyRepository(db_session).get(workspace_id)
        policy.memory_enabled = False
        await MemoryRepository(db_session).create(
            workspace_id=workspace_id,
            user_id=await _me(client, headers),
            scope="user",
            kind="preference",
            content=_MEMORY,
            confidence=0.9,
            embedding=_TONE,
            embedding_model=test_settings.embedding_model,
        )
        await db_session.flush()

    models, _, _ = await _run_with(
        client, db_session, redis_client, test_settings, query_vector=_TONE, seed=seed
    )

    assert not any(_MEMORY in prompt for prompt in _system_prompts(models))
