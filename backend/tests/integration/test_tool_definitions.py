"""`tool_definitions` (Phase 6, docs/system-design.md section 14.3): tenant scoping in the
repository, the constraints the sync service will lean on, and a clean migration round trip.
"""

import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from relay_core.db.models.tools import ToolDefinition
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceRepository

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


async def _workspace_with_installation(
    session: AsyncSession, label: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user = await UserRepository(session).create(
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com", full_name=label, password_hash="x"
    )
    await session.flush()
    workspace = await WorkspaceRepository(session).create(
        name=label, slug=f"{label}-{uuid.uuid4().hex[:8]}", created_by=user.id
    )
    installation = await ConnectorInstallationRepository(session).create(
        workspace_id=workspace.id,
        connector_key="mcp",
        name="Tickets",
        slug="tickets",
        config={},
        priority=100,
        installed_by=user.id,
    )
    return workspace.id, installation.id, user.id


def _tool(workspace_id: uuid.UUID, installation_id: uuid.UUID, name: str) -> ToolDefinition:
    return ToolDefinition(
        workspace_id=workspace_id,
        installation_id=installation_id,
        name=name,
        llm_name=f"tickets__{name}",
        description="Search tickets",
        input_schema={"type": "object"},
        schema_hash="abc",
        risk="write",
    )


async def test_rows_are_invisible_to_other_workspaces_and_die_with_their_installation(
    db_session: AsyncSession,
) -> None:
    ws_a, installation_a, _ = await _workspace_with_installation(db_session, "tools-a")
    ws_b, _, _ = await _workspace_with_installation(db_session, "tools-b")
    tools = ToolDefinitionRepository(db_session)
    tool = await tools.add(_tool(ws_a, installation_a, "search_tickets"))

    assert await tools.get(ws_b, tool.id) is None
    assert await tools.list_for_installation(ws_b, installation_a) == []
    assert [t.id for t in await tools.list_for_installation(ws_a, installation_a)] == [tool.id]

    await ConnectorInstallationRepository(db_session).delete(ws_a, installation_a)
    await db_session.flush()
    assert await tools.list_for_installation(ws_a, installation_a) == []


async def test_llm_name_is_unique_within_a_workspace(db_session: AsyncSession) -> None:
    workspace_id, installation_id, user_id = await _workspace_with_installation(
        db_session, "tools-dup"
    )
    other = await ConnectorInstallationRepository(db_session).create(
        workspace_id=workspace_id,
        connector_key="mcp",
        name="Tickets 2",
        slug="tickets-2",
        config={},
        priority=100,
        installed_by=user_id,
    )
    tools = ToolDefinitionRepository(db_session)
    await tools.add(_tool(workspace_id, installation_id, "search_tickets"))

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await tools.add(_tool(workspace_id, other.id, "search_tickets"))


def test_migration_downgrades_and_upgrades_cleanly(migrated_db_url: str) -> None:
    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", migrated_db_url)
    command.downgrade(cfg, "c7a41b9e5d20")
    command.upgrade(cfg, "head")
