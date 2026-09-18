"""Tool retrieval (Phase 6 C3): with more installed tools than `limit`, a query binds only the
nearest ones by embedding."""

import math
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.tools import ToolDefinition
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceRepository
from relay_core.security.crypto import LocalKMS
from relay_core.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio

_QUERY = [1.0] + [0.0] * 767


def _at(degrees: float) -> list[float]:
    """A unit vector `degrees` away from `_QUERY`: nearer means a smaller angle."""
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians)] + [0.0] * 766


class _FixedEmbeddings:
    async def embed(self, texts: list[str], **_: object) -> list[list[float]]:
        return [_QUERY for _ in texts]


async def _tools(
    session: AsyncSession, embeddings: list[list[float] | None]
) -> tuple[uuid.UUID, uuid.UUID]:
    user = await UserRepository(session).create(
        email=f"retrieval-{uuid.uuid4().hex[:8]}@example.com", full_name="R", password_hash="x"
    )
    await session.flush()
    workspace = await WorkspaceRepository(session).create(
        name="Retrieval", slug=f"retrieval-{uuid.uuid4().hex[:8]}", created_by=user.id
    )
    installations = ConnectorInstallationRepository(session)
    installation = await installations.create(
        workspace_id=workspace.id,
        connector_key="mcp",
        name="Many",
        slug="many",
        config={"url": "https://tools.example/mcp"},
        priority=100,
        installed_by=user.id,
    )
    await installations.set_health(workspace.id, installation.id, health="healthy", message="ok")
    repo = ToolDefinitionRepository(session)
    for i, embedding in enumerate(embeddings):
        await repo.add(
            ToolDefinition(
                workspace_id=workspace.id,
                installation_id=installation.id,
                name=f"tool_{i:02d}",
                llm_name=f"many__tool_{i:02d}",
                description=f"Tool {i}",
                input_schema={"type": "object", "properties": {}},
                schema_hash=str(i),
                risk="read",
                capabilities=["custom.many.read"],
                embedding=embedding,
            )
        )
    return workspace.id, user.id


async def _bound(
    session: AsyncSession, test_settings, workspace_id, user_id, **kwargs: object
) -> set[str]:
    registry = ToolRegistry(
        session,
        None,  # type: ignore[arg-type]
        LocalKMS(os.urandom(32)),
        _FixedEmbeddings(),  # type: ignore[arg-type]
        test_settings,
    )
    tools = await registry.tools_for_run(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        capabilities=["custom.many.read"],
        **kwargs,  # type: ignore[arg-type]
    )
    return {t.llm_name for t in tools if t.installation_id is not None}


async def test_a_query_binds_only_the_nearest_tools(
    db_session: AsyncSession, test_settings
) -> None:
    workspace_id, user_id = await _tools(db_session, [_at(i * 3) for i in range(30)])

    nearest = await _bound(
        db_session, test_settings, workspace_id, user_id, query="find things", limit=5
    )
    assert nearest == {f"many__tool_{i:02d}" for i in range(5)}

    everything = await _bound(db_session, test_settings, workspace_id, user_id, limit=5)
    assert len(everything) == 30


async def test_an_unembedded_tool_sorts_last_but_still_binds_when_there_is_room(
    db_session: AsyncSession, test_settings
) -> None:
    workspace_id, user_id = await _tools(db_session, [None, None, _at(80), None])

    bound = await _bound(db_session, test_settings, workspace_id, user_id, query="q", limit=3)
    assert "many__tool_02" in bound
    assert len(bound) == 3


async def test_with_retrieval_off_every_tool_binds(db_session: AsyncSession, test_settings) -> None:
    """Experiment 2 (section 21.6): `TOOL_RETRIEVAL_ENABLED=false` binds the whole set."""
    workspace_id, user_id = await _tools(db_session, [_at(i * 3) for i in range(30)])
    off = test_settings.model_copy(update={"tool_retrieval_enabled": False})
    bound = await _bound(db_session, off, workspace_id, user_id, query="find things", limit=5)
    assert len(bound) == 30
