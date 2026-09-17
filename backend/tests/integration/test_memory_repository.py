"""`memories` rows and their scope rule (Phase 7 A2, docs/system-design.md section 12)."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.repositories.memories import MemoryRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceRepository

pytestmark = pytest.mark.asyncio

_NEAR = [1.0] + [0.0] * 767
_FAR = [0.0] * 767 + [1.0]


async def _user(session: AsyncSession, name: str) -> uuid.UUID:
    user = await UserRepository(session).create(
        email=f"{name}-{uuid.uuid4().hex[:8]}@example.com", full_name=name, password_hash="x"
    )
    await session.flush()
    return user.id


async def _workspace(session: AsyncSession, owner_id: uuid.UUID) -> uuid.UUID:
    workspace = await WorkspaceRepository(session).create(
        name="Memory", slug=f"memory-{uuid.uuid4().hex[:8]}", created_by=owner_id
    )
    await session.flush()
    return workspace.id


async def _memory(
    repo: MemoryRepository,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    content: str,
    *,
    scope: str = "user",
    embedding: list[float] | None = None,
) -> uuid.UUID:
    memory = await repo.create(
        workspace_id=workspace_id,
        user_id=user_id,
        scope=scope,
        kind="preference",
        content=content,
        confidence=0.9,
        embedding=embedding or _NEAR,
        embedding_model="gemini-embedding-001",
    )
    return memory.id


async def test_nearest_returns_own_and_workspace_memories_but_not_another_users(
    db_session: AsyncSession,
) -> None:
    repo = MemoryRepository(db_session)
    owner_id = await _user(db_session, "owner")
    other_id = await _user(db_session, "other")
    workspace_id = await _workspace(db_session, owner_id)

    mine = await _memory(repo, workspace_id, owner_id, "Prefers formal drafts")
    shared = await _memory(repo, workspace_id, None, "Renewals close on the 1st", scope="workspace")
    await _memory(repo, workspace_id, other_id, "Prefers bullet points")
    # The same person in a different workspace: memory never crosses the tenant boundary.
    elsewhere_id = await _workspace(db_session, owner_id)
    await _memory(repo, elsewhere_id, owner_id, "Prefers tables")

    found = await repo.nearest(workspace_id, owner_id, _NEAR, limit=5)

    assert {m.id for m in found} == {mine, shared}


async def test_nearest_skips_inactive_rows_and_anything_past_the_distance_cap(
    db_session: AsyncSession,
) -> None:
    repo = MemoryRepository(db_session)
    owner_id = await _user(db_session, "owner")
    workspace_id = await _workspace(db_session, owner_id)

    close = await _memory(repo, workspace_id, owner_id, "Close")
    silenced = await _memory(repo, workspace_id, owner_id, "Silenced")
    await _memory(repo, workspace_id, owner_id, "Unrelated", embedding=_FAR)
    await repo.update(await repo.get(workspace_id, silenced), is_active=False)

    found = await repo.nearest(workspace_id, owner_id, _NEAR, limit=5)

    assert [m.id for m in found] == [close]
    assert len(await repo.list_visible_to(workspace_id, owner_id, include_inactive=True)) == 3


async def test_mark_used_counts_the_memories_a_run_actually_used(
    db_session: AsyncSession,
) -> None:
    repo = MemoryRepository(db_session)
    owner_id = await _user(db_session, "owner")
    workspace_id = await _workspace(db_session, owner_id)
    used = await _memory(repo, workspace_id, owner_id, "Used")
    unused = await _memory(repo, workspace_id, owner_id, "Unused")

    await repo.mark_used(workspace_id, [used])
    await db_session.flush()
    await db_session.refresh(await repo.get(workspace_id, used))

    assert (await repo.get(workspace_id, used)).use_count == 1
    assert (await repo.get(workspace_id, used)).last_used_at is not None
    assert (await repo.get(workspace_id, unused)).use_count == 0


async def test_the_database_refuses_a_workspace_memory_owned_by_one_user(
    db_session: AsyncSession,
) -> None:
    """A workspace memory belongs to everybody in the workspace; the check constraint is what
    keeps `nearest`'s scope rule from being bypassed by a badly built row."""
    repo = MemoryRepository(db_session)
    owner_id = await _user(db_session, "owner")
    workspace_id = await _workspace(db_session, owner_id)

    with pytest.raises(IntegrityError):
        await repo.create(
            workspace_id=workspace_id,
            user_id=None,
            scope="user",
            kind="fact",
            content="Nobody's memory",
            confidence=0.9,
            embedding=_NEAR,
            embedding_model="gemini-embedding-001",
        )
