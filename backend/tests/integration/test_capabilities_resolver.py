"""Coverage for `relay_core.capabilities.resolver.resolve_available_capabilities`
(docs/system-design.md section 7.2), called by `load_context` but never tested directly.
`file.read` is always present, an installation only contributes its enabled `tool_definitions`
rows' capabilities (custom ones included) while `active`+`healthy`/`degraded`, and an
attachment's inferred capabilities count even with zero connectors installed.
"""

import os
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.capabilities.resolver import resolve_available_capabilities
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.collections import CollectionRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.security.crypto import LocalKMS
from relay_core.tools.sync import sync_installation

pytestmark = pytest.mark.asyncio


async def _register_workspace_conversation_and_user(
    client: AsyncClient, email: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    register_resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert register_resp.status_code == 201, register_resp.text
    headers = {"Authorization": f"Bearer {register_resp.json()['access_token']}"}

    me_resp = await client.get("/api/v1/auth/me", headers=headers)
    user_id = uuid.UUID(me_resp.json()["id"])

    ws_resp = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
    workspace_id = uuid.UUID(ws_resp.json()["id"])

    conv_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={}, headers=headers
    )
    conversation_id = uuid.UUID(conv_resp.json()["id"])

    return workspace_id, user_id, conversation_id


async def test_only_file_read_when_nothing_is_installed_or_attached(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, _, conversation_id = await _register_workspace_conversation_and_user(
        client, "resolver-empty@example.com"
    )
    available = await resolve_available_capabilities(
        ToolDefinitionRepository(db_session),
        AttachmentRepository(db_session),
        DocumentRepository(db_session),
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    assert available == ["file.read"]


async def test_a_healthy_active_postgres_installation_contributes_its_rows_capabilities(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_conversation_and_user(
        client, "resolver-healthy@example.com"
    )
    installations = ConnectorInstallationRepository(db_session)
    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key="postgres",
        name="Sales DB",
        slug="sales-db",
        config={"host": "db", "port": 5432, "database": "sales"},
        priority=100,
        installed_by=user_id,
    )
    await sync_installation(db_session, LocalKMS(os.urandom(32)), installation)
    await installations.set_health(workspace_id, installation.id, health="healthy", message="ok")

    available = await resolve_available_capabilities(
        ToolDefinitionRepository(db_session),
        AttachmentRepository(db_session),
        DocumentRepository(db_session),
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    assert available == [
        "customer.read",
        "file.read",
        "sql.query",
        "subscription.read",
        "usage.read",
    ]


async def test_a_down_installation_is_excluded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_conversation_and_user(
        client, "resolver-down@example.com"
    )
    installations = ConnectorInstallationRepository(db_session)
    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key="postgres",
        name="Sales DB",
        slug="sales-db",
        config={},
        priority=100,
        installed_by=user_id,
    )
    await sync_installation(db_session, LocalKMS(os.urandom(32)), installation)
    await installations.set_health(workspace_id, installation.id, health="down", message="refused")

    available = await resolve_available_capabilities(
        ToolDefinitionRepository(db_session),
        AttachmentRepository(db_session),
        DocumentRepository(db_session),
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    assert available == ["file.read"]


async def test_a_disabled_installation_is_excluded_even_if_healthy(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_conversation_and_user(
        client, "resolver-disabled@example.com"
    )
    installations = ConnectorInstallationRepository(db_session)
    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key="postgres",
        name="Sales DB",
        slug="sales-db",
        config={},
        priority=100,
        installed_by=user_id,
    )
    await sync_installation(db_session, LocalKMS(os.urandom(32)), installation)
    await installations.set_health(
        workspace_id, installation.id, health="healthy", message="ok", status="disabled"
    )

    available = await resolve_available_capabilities(
        ToolDefinitionRepository(db_session),
        AttachmentRepository(db_session),
        DocumentRepository(db_session),
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    assert available == ["file.read"]


async def test_a_degraded_installation_still_counts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_conversation_and_user(
        client, "resolver-degraded@example.com"
    )
    installations = ConnectorInstallationRepository(db_session)
    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key="postgres",
        name="Sales DB",
        slug="sales-db",
        config={},
        priority=100,
        installed_by=user_id,
    )
    await sync_installation(db_session, LocalKMS(os.urandom(32)), installation)
    await installations.set_health(workspace_id, installation.id, health="degraded", message="slow")

    available = await resolve_available_capabilities(
        ToolDefinitionRepository(db_session),
        AttachmentRepository(db_session),
        DocumentRepository(db_session),
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    assert "sql.query" in available


async def test_an_attachments_inferred_capabilities_count_with_zero_connectors(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_conversation_and_user(
        client, "resolver-csv@example.com"
    )
    await AttachmentRepository(db_session).create(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        filename="usage.csv",
        mime_type="text/csv",
        size_bytes=100,
        blob_key="attachments/usage.csv",
        kind="table",
        profile={"columns": ["account_name", "active_users"]},
        inferred_capabilities=["usage.read"],
        uploaded_by=user_id,
    )

    available = await resolve_available_capabilities(
        ToolDefinitionRepository(db_session),
        AttachmentRepository(db_session),
        DocumentRepository(db_session),
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    assert available == ["file.read", "usage.read"]


async def test_knowledge_search_only_appears_once_a_document_is_ready(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_conversation_and_user(
        client, "resolver-knowledge@example.com"
    )
    documents = DocumentRepository(db_session)
    collection = await CollectionRepository(db_session).get_or_create_default(workspace_id)
    document = await documents.create(
        workspace_id=workspace_id,
        collection_id=collection.id,
        title="Playbook",
        blob_key="docs/playbook.md",
        mime_type="text/markdown",
        size_bytes=10,
        sha256="abc",
        uploaded_by=user_id,
    )

    still_queued = await resolve_available_capabilities(
        ToolDefinitionRepository(db_session),
        AttachmentRepository(db_session),
        documents,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    assert "knowledge.search" not in still_queued

    await documents.mark_ready(workspace_id, document.id, page_count=None, chunk_count=1)

    now_ready = await resolve_available_capabilities(
        ToolDefinitionRepository(db_session),
        AttachmentRepository(db_session),
        documents,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    assert "knowledge.search" in now_ready


async def test_a_custom_capability_on_a_row_is_available(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_conversation_and_user(
        client, "resolver-custom@example.com"
    )
    installations = ConnectorInstallationRepository(db_session)
    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key="postgres",
        name="Sales DB",
        slug="sales-db",
        config={},
        priority=100,
        installed_by=user_id,
    )
    await sync_installation(db_session, LocalKMS(os.urandom(32)), installation)
    await installations.set_health(workspace_id, installation.id, health="healthy", message="ok")
    tools = ToolDefinitionRepository(db_session)
    for row in await tools.list_for_installation(workspace_id, installation.id):
        row.capabilities = ["custom.ticket.read"]
    await db_session.flush()

    available = await resolve_available_capabilities(
        tools,
        AttachmentRepository(db_session),
        DocumentRepository(db_session),
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    assert available == ["custom.ticket.read", "file.read"]
