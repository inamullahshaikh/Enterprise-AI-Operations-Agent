"""Tool review, the capability map and installation priority (Phase 6 C2)."""

import os
import uuid

import pytest
from google.genai import types
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.agent.nodes.execute_step import requires_approval
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.policies import WorkspacePolicyRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.security.crypto import LocalKMS
from relay_core.tools.registry import ToolRegistry
from relay_core.tools.sync import sync_installation

pytestmark = pytest.mark.asyncio


async def _register(client: AsyncClient, email: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": email},
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _workspace(client: AsyncClient, email: str) -> tuple[dict, uuid.UUID, uuid.UUID]:
    headers = await _register(client, email)
    user_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=headers)).json()["id"])
    resp = await client.post("/api/v1/workspaces", json={"name": "Tools Co"}, headers=headers)
    return headers, uuid.UUID(resp.json()["id"]), user_id


async def _postgres(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, slug: str
) -> uuid.UUID:
    installations = ConnectorInstallationRepository(session)
    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key="postgres",
        name=slug,
        slug=slug,
        config={"host": "db", "port": 5432, "database": "sales"},
        priority=100,
        installed_by=user_id,
    )
    await sync_installation(session, LocalKMS(os.urandom(32)), installation)
    await installations.set_health(workspace_id, installation.id, health="healthy", message="ok")
    return installation.id


async def test_the_tool_listing_filters(client: AsyncClient, db_session: AsyncSession) -> None:
    headers, workspace_id, user_id = await _workspace(client, "tools-list@example.com")
    installation_id = await _postgres(db_session, workspace_id, user_id, "sales-db")
    other_id = await _postgres(db_session, workspace_id, user_id, "other-db")
    tools = ToolDefinitionRepository(db_session)
    [run_sql] = [
        t
        for t in await tools.list_for_installation(workspace_id, installation_id)
        if t.name == "run_sql"
    ]
    run_sql.risk, run_sql.needs_review, run_sql.capabilities = "write", True, ["custom.sql.write"]
    await db_session.flush()
    url = f"/api/v1/workspaces/{workspace_id}/tools"

    async def names(**params: str) -> list[str]:
        resp = await client.get(url, params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        return [t["llm_name"] for t in resp.json()]

    assert len(await names()) == 6
    assert await names(risk="write") == ["sales-db__run_sql"]
    assert await names(needs_review="true") == ["sales-db__run_sql"]
    assert await names(capability="custom.sql.write") == ["sales-db__run_sql"]
    assert await names(installation_id=str(other_id)) == [
        "other-db__describe_table",
        "other-db__list_tables",
        "other-db__run_sql",
    ]


async def test_lowering_a_tools_risk_means_the_next_run_needs_no_approval(
    client: AsyncClient, db_session: AsyncSession, test_settings
) -> None:
    headers, workspace_id, user_id = await _workspace(client, "tools-risk@example.com")
    installation_id = await _postgres(db_session, workspace_id, user_id, "sales-db")
    tools = ToolDefinitionRepository(db_session)
    [run_sql] = [
        t
        for t in await tools.list_for_installation(workspace_id, installation_id)
        if t.name == "run_sql"
    ]
    run_sql.risk = "write"
    await db_session.flush()

    registry = ToolRegistry(
        db_session,
        None,
        LocalKMS(os.urandom(32)),
        None,
        test_settings,
    )
    rules = await WorkspacePolicyRepository(db_session).approval_rules(workspace_id)
    call = types.FunctionCall(name="sales-db__run_sql", args={"sql": "SELECT 1"})

    async def gated() -> bool:
        bound = await registry.tools_for_run(
            workspace_id=workspace_id,
            user_id=user_id,
            run_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            capabilities=["sql.query"],
        )
        return requires_approval(bound.lookup("sales-db__run_sql"), call, rules, "owner")

    assert await gated() is True
    resp = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/tools/{run_sql.id}",
        json={"risk": "read", "reviewed": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["risk"], resp.json()["risk_overridden"]) == ("read", True)
    assert await gated() is False


async def test_a_member_cannot_edit_a_tool(client: AsyncClient, db_session: AsyncSession) -> None:
    headers, workspace_id, user_id = await _workspace(client, "tools-owner@example.com")
    installation_id = await _postgres(db_session, workspace_id, user_id, "sales-db")
    [tool, *_] = await ToolDefinitionRepository(db_session).list_for_installation(
        workspace_id, installation_id
    )
    member_headers = await _register(client, "tools-member@example.com")
    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"email": "tools-member@example.com", "role": "member"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/tools/{tool.id}",
        json={"is_enabled": False},
        headers=member_headers,
    )
    assert resp.status_code == 403


async def test_invalid_capabilities_are_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers, workspace_id, user_id = await _workspace(client, "tools-caps@example.com")
    installation_id = await _postgres(db_session, workspace_id, user_id, "sales-db")
    [tool, *_] = await ToolDefinitionRepository(db_session).list_for_installation(
        workspace_id, installation_id
    )
    resp = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/tools/{tool.id}",
        json={"capabilities": ["sql.query", "Tickets!"]},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "Tickets!" in resp.json()["detail"]


async def test_the_capability_map_winner_follows_priority(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers, workspace_id, user_id = await _workspace(client, "tools-map@example.com")
    older = await _postgres(db_session, workspace_id, user_id, "older-db")
    await _postgres(db_session, workspace_id, user_id, "newer-db")

    async def sql_query() -> dict:
        resp = await client.get(f"/api/v1/workspaces/{workspace_id}/capabilities", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "email.send" in body["gaps"] and "sql.query" not in body["gaps"]
        [entry] = [c for c in body["capabilities"] if c["capability"] == "sql.query"]
        return {p["slug"]: p for p in entry["providers"]}

    providers = await sql_query()
    assert providers["newer-db"]["winner"] and not providers["older-db"]["winner"]
    assert providers["newer-db"]["tool_count"] == 3

    resp = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/connectors/{older}",
        json={"priority": 1},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    providers = await sql_query()
    assert providers["older-db"]["winner"] and not providers["newer-db"]["winner"]
