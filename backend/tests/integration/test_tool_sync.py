"""Tool discovery sync (Phase 6 A3, `relay_core.tools.sync`): installing writes
`tool_definitions` rows, a re-sync upserts them without clobbering admin state, and a failing
`list_tools` marks the installation down without wiping its rows.
"""

import os
import uuid
from typing import Any, ClassVar

import pytest
from httpx import AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.connectors.base import (
    AuthType,
    Connector,
    ExecutionContext,
    Risk,
    ToolResult,
    ToolSpec,
)
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceRepository
from relay_core.security.crypto import LocalKMS
from relay_core.tools.sync import sync_installation

pytestmark = pytest.mark.asyncio


class _FakeConnector(Connector):
    key = "fake"
    display_name = "Fake"
    auth_type = AuthType.NONE
    specs: ClassVar[list[ToolSpec]] = []
    fail: ClassVar[bool] = False

    async def list_tools(self, ctx: ExecutionContext) -> list[ToolSpec]:
        if self.fail:
            raise RuntimeError("server unreachable")
        return list(self.specs)

    async def call_tool(
        self, ctx: ExecutionContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult:
        raise NotImplementedError

    async def health_check(self, ctx: ExecutionContext) -> tuple[bool, str]:
        return True, "ok"


def _spec(name: str, capabilities: list[str] | None = None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} things",
        input_schema={"type": "object", "properties": {}},
        risk=Risk.READ,
        capabilities=capabilities or [],
    )


@pytest.fixture
def fake_connector(monkeypatch: pytest.MonkeyPatch) -> type[_FakeConnector]:
    monkeypatch.setitem(CONNECTOR_TYPES, "fake", _FakeConnector)
    monkeypatch.setattr(_FakeConnector, "specs", [])
    monkeypatch.setattr(_FakeConnector, "fail", False)
    return _FakeConnector


async def _fake_installation(session: AsyncSession) -> ConnectorInstallation:
    user = await UserRepository(session).create(
        email=f"sync-{uuid.uuid4().hex[:8]}@example.com", full_name="Sync", password_hash="x"
    )
    await session.flush()
    workspace = await WorkspaceRepository(session).create(
        name="Sync Co", slug=f"sync-{uuid.uuid4().hex[:8]}", created_by=user.id
    )
    return await ConnectorInstallationRepository(session).create(
        workspace_id=workspace.id,
        connector_key="fake",
        name="Fake",
        slug="fake",
        config={},
        priority=100,
        installed_by=user.id,
    )


async def test_installing_postgres_writes_declared_rows_and_a_resync_changes_nothing(
    client: AsyncClient, db_session: AsyncSession, postgres_url: str
) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "sync-postgres@example.com",
            "password": "correct horse battery staple",
            "full_name": "Sync",
        },
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    resp = await client.post("/api/v1/workspaces", json={"name": "Sync PG"}, headers=headers)
    workspace_id = resp.json()["id"]
    url = make_url(postgres_url)
    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json={
            "connector_key": "postgres",
            "name": "Sales DB",
            "config": {"host": url.host, "port": url.port, "database": url.database},
            "secrets": {"username": url.username, "password": url.password},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    installation_id = uuid.UUID(resp.json()["id"])

    rows = await ToolDefinitionRepository(db_session).list_for_installation(
        uuid.UUID(workspace_id), installation_id
    )
    assert {r.llm_name for r in rows} == {
        "sales-db__describe_table",
        "sales-db__list_tables",
        "sales-db__run_sql",
    }
    assert all(r.capabilities and r.capability_source == "declared" for r in rows)
    assert not any(r.needs_review for r in rows)

    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors/{installation_id}/sync", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "added": [],
        "updated": [],
        "removed": [],
        "needs_review": [],
        "error": None,
    }


async def test_a_tool_removed_upstream_loses_its_row(
    db_session: AsyncSession, fake_connector: type[_FakeConnector]
) -> None:
    installation = await _fake_installation(db_session)
    kms = LocalKMS(os.urandom(32))
    fake_connector.specs = [_spec("keep", ["sql.query"]), _spec("drop")]

    report = await sync_installation(db_session, kms, installation)
    assert report.added == ["keep", "drop"]
    assert report.needs_review == ["drop"]
    assert installation.last_synced_at is not None

    fake_connector.specs = [_spec("keep", ["sql.query"])]
    report = await sync_installation(db_session, kms, installation)
    assert (report.removed, report.updated) == (["drop"], [])
    rows = await ToolDefinitionRepository(db_session).list_for_installation(
        installation.workspace_id, installation.id
    )
    assert [r.name for r in rows] == ["keep"]


async def test_an_admin_risk_override_survives_a_sync(
    db_session: AsyncSession, fake_connector: type[_FakeConnector]
) -> None:
    installation = await _fake_installation(db_session)
    kms = LocalKMS(os.urandom(32))
    fake_connector.specs = [_spec("lookup", ["sql.query"])]
    await sync_installation(db_session, kms, installation)

    tools = ToolDefinitionRepository(db_session)
    [row] = await tools.list_for_installation(installation.workspace_id, installation.id)
    row.risk, row.risk_overridden = "destructive", True
    await db_session.flush()

    report = await sync_installation(db_session, kms, installation)
    assert report.updated == []
    [row] = await tools.list_for_installation(installation.workspace_id, installation.id)
    assert row.risk == "destructive"


async def test_a_failing_list_tools_keeps_rows_and_marks_the_installation_down(
    db_session: AsyncSession, fake_connector: type[_FakeConnector]
) -> None:
    installation = await _fake_installation(db_session)
    kms = LocalKMS(os.urandom(32))
    fake_connector.specs = [_spec("lookup", ["sql.query"])]
    await sync_installation(db_session, kms, installation)

    fake_connector.fail = True
    report = await sync_installation(db_session, kms, installation)
    assert report.error is not None and "server unreachable" in report.error
    assert installation.health == "down"
    rows = await ToolDefinitionRepository(db_session).list_for_installation(
        installation.workspace_id, installation.id
    )
    assert [r.name for r in rows] == ["lookup"]
