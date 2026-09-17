"""The memories API (Phase 7 D3, docs/system-design.md section 15.5).

The edit test asserts on retrieval behaviour rather than on the stored vector: an edit that did
not re-embed would still return 200 with the new text, and the row would go on being recalled by
the *old* text's queries. `MemoryRepository.nearest` is the only thing that can tell the
difference, so that is what the test asks.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_llm_gateway
from relay_api.main import app
from relay_core.config import Settings
from relay_core.db.repositories.memories import MemoryRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository

pytestmark = pytest.mark.asyncio

_A = [1.0] + [0.0] * 767
_B = [0.0] * 767 + [1.0]


class _FakeGateway:
    """Only `embed` is reachable from this router, and only on a content edit."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls = 0

    async def embed(
        self, texts: list[str], *, task: str, settings: Settings
    ) -> list[list[float]]:
        self.calls += 1
        return [self.vector for _ in texts]


@pytest_asyncio.fixture
async def embed_gateway(client: AsyncClient):
    """Edits re-embed, and Gemini is unreachable in integration tests."""
    gateway = _FakeGateway(_B)
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    try:
        yield gateway
    finally:
        app.dependency_overrides.pop(get_llm_gateway, None)


async def _register(client: AsyncClient, email: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _workspace(client: AsyncClient, headers: dict) -> uuid.UUID:
    resp = await client.post("/api/v1/workspaces", json={"name": "Memory Co"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _me(client: AsyncClient, headers: dict) -> uuid.UUID:
    resp = await client.get("/api/v1/auth/me", headers=headers)
    return uuid.UUID(resp.json()["id"])


async def _memory(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    content: str,
    *,
    scope: str = "user",
    kind: str = "preference",
    embedding: list[float] | None = None,
) -> uuid.UUID:
    memory = await MemoryRepository(session).create(
        workspace_id=workspace_id,
        user_id=user_id,
        scope=scope,
        kind=kind,
        content=content,
        confidence=0.9,
        embedding=embedding or _A,
        embedding_model="gemini-embedding-001",
    )
    await session.flush()
    return memory.id


async def test_list_edit_deactivate_and_delete_round_trip(
    client: AsyncClient, db_session: AsyncSession, embed_gateway: _FakeGateway
) -> None:
    headers = await _register(client, "owner@example.com")
    workspace_id = await _workspace(client, headers)
    user_id = await _me(client, headers)
    memory_id = await _memory(db_session, workspace_id, user_id, "Prefers formal drafts")
    await _memory(db_session, workspace_id, None, "Renewals close on the 1st", scope="workspace")

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/memories", headers=headers)
    assert listed.status_code == 200, listed.text
    assert {m["content"] for m in listed.json()} == {
        "Prefers formal drafts",
        "Renewals close on the 1st",
    }
    assert "embedding" not in listed.json()[0]

    edited = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/memories/{memory_id}",
        json={"content": "Prefers terse drafts", "kind": "fact"},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["content"] == "Prefers terse drafts"
    assert edited.json()["kind"] == "fact"

    deactivated = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/memories/{memory_id}",
        json={"is_active": False},
        headers=headers,
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False
    active = await client.get(f"/api/v1/workspaces/{workspace_id}/memories", headers=headers)
    assert [m["content"] for m in active.json()] == ["Renewals close on the 1st"]

    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/memories/{memory_id}", headers=headers
    )
    assert deleted.status_code == 204
    assert await MemoryRepository(db_session).get(workspace_id, memory_id) is None


async def test_editing_the_content_changes_what_the_row_is_recalled_by(
    client: AsyncClient, db_session: AsyncSession, embed_gateway: _FakeGateway
) -> None:
    headers = await _register(client, "owner@example.com")
    workspace_id = await _workspace(client, headers)
    user_id = await _me(client, headers)
    memory_id = await _memory(db_session, workspace_id, user_id, "Prefers formal drafts")

    repo = MemoryRepository(db_session)
    assert [m.id for m in await repo.nearest(workspace_id, user_id, _A)] == [memory_id]

    edited = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/memories/{memory_id}",
        json={"content": "Owns the EMEA accounts"},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert embed_gateway.calls == 1

    assert await repo.nearest(workspace_id, user_id, _A) == []
    assert [m.id for m in await repo.nearest(workspace_id, user_id, _B)] == [memory_id]


async def test_a_member_cannot_edit_or_delete_a_workspace_memory(
    client: AsyncClient, db_session: AsyncSession, embed_gateway: _FakeGateway
) -> None:
    owner_headers = await _register(client, "owner@example.com")
    workspace_id = await _workspace(client, owner_headers)
    member_headers = await _register(client, "member@example.com")
    member_id = await _me(client, member_headers)
    await WorkspaceMemberRepository(db_session).add(
        workspace_id=workspace_id, user_id=member_id, role="member"
    )
    await db_session.flush()
    memory_id = await _memory(
        db_session, workspace_id, None, "Renewals close on the 1st", scope="workspace"
    )

    # Visible to them...
    listed = await client.get(
        f"/api/v1/workspaces/{workspace_id}/memories", headers=member_headers
    )
    assert [m["id"] for m in listed.json()] == [str(memory_id)]

    # ...but not theirs to change.
    edited = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/memories/{memory_id}",
        json={"content": "Renewals close on the 15th"},
        headers=member_headers,
    )
    assert edited.status_code == 403
    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/memories/{memory_id}", headers=member_headers
    )
    assert deleted.status_code == 403

    # The owner may.
    assert (
        await client.delete(
            f"/api/v1/workspaces/{workspace_id}/memories/{memory_id}", headers=owner_headers
        )
    ).status_code == 204


async def test_another_members_memory_is_404_not_403(
    client: AsyncClient, db_session: AsyncSession, embed_gateway: _FakeGateway
) -> None:
    """Confirming that someone else's memory exists is itself a leak of what Relay knows about
    them, so this is the one case that hides behind a 404."""
    owner_headers = await _register(client, "owner@example.com")
    workspace_id = await _workspace(client, owner_headers)
    member_headers = await _register(client, "member@example.com")
    member_id = await _me(client, member_headers)
    await WorkspaceMemberRepository(db_session).add(
        workspace_id=workspace_id, user_id=member_id, role="member"
    )
    await db_session.flush()
    owner_memory = await _memory(
        db_session, workspace_id, await _me(client, owner_headers), "Prefers formal drafts"
    )

    listed = await client.get(
        f"/api/v1/workspaces/{workspace_id}/memories", headers=member_headers
    )
    assert listed.json() == []
    edited = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/memories/{owner_memory}",
        json={"is_active": False},
        headers=member_headers,
    )
    assert edited.status_code == 404


async def test_kind_and_query_filters_narrow_the_list(
    client: AsyncClient, db_session: AsyncSession, embed_gateway: _FakeGateway
) -> None:
    headers = await _register(client, "owner@example.com")
    workspace_id = await _workspace(client, headers)
    user_id = await _me(client, headers)
    await _memory(db_session, workspace_id, user_id, "Prefers formal drafts")
    await _memory(db_session, workspace_id, user_id, "Owns the EMEA accounts", kind="fact")

    by_kind = await client.get(
        f"/api/v1/workspaces/{workspace_id}/memories", params={"kind": "fact"}, headers=headers
    )
    assert [m["content"] for m in by_kind.json()] == ["Owns the EMEA accounts"]

    by_q = await client.get(
        f"/api/v1/workspaces/{workspace_id}/memories", params={"q": "formal"}, headers=headers
    )
    assert [m["content"] for m in by_q.json()] == ["Prefers formal drafts"]

    bad_kind = await client.get(
        f"/api/v1/workspaces/{workspace_id}/memories", params={"kind": "nonsense"}, headers=headers
    )
    assert bad_kind.status_code == 422
