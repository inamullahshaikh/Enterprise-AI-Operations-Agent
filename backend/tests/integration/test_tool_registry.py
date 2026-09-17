"""Coverage for `relay_core.tools.registry.ToolRegistry` (docs/system-design.md section 6.7),
which `execute_step` relies on but nothing tests directly: binding `file_upload`/`documents`
(always) plus installed connectors' enabled `tool_definitions` rows that provide a requested
capability, only from the best-priority installation per capability, with the row's
`{slug}__{tool_name}` name (section 6.6), decrypted credentials in the bound `ExecutionContext`,
and nothing from installations that aren't active+healthy. Rows come from
`relay_core.tools.sync.sync_installation`; `postgres.list_tools` is pure (no I/O), so syncing one
needs no live database.
"""

import os
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.capabilities.resolver import resolve_available_capabilities
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.llm_calls import LLMCallRepository, ModelPricingRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.ratelimit import RedisRateLimiter
from relay_core.security.credential_codec import encrypt_secrets
from relay_core.security.crypto import LocalKMS
from relay_core.tools.registry import ToolRegistry
from relay_core.tools.sync import sync_installation

pytestmark = pytest.mark.asyncio


class _FakeObjectStore:
    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        raise NotImplementedError

    async def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


class _UnusedClient:
    """`list_tools()` never calls the gateway (only `call_tool` does, e.g. `documents`'
    `search_documents`), so every test in this file only needs a gateway that constructs
    successfully — never one that actually generates or embeds anything."""


def _gateway(db_session: AsyncSession, redis_client: Redis, test_settings) -> LLMGateway:
    return LLMGateway(
        _UnusedClient(),  # type: ignore[arg-type]
        limiter=RedisRateLimiter(redis_client, rpm_limit=test_settings.gemini_rpm_limit),
        llm_calls=LLMCallRepository(db_session),
        pricing=ModelPricingRepository(db_session),
    )


async def _register_workspace_and_user(
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


def _registry(db_session: AsyncSession, redis_client: Redis, test_settings) -> ToolRegistry:
    return ToolRegistry(
        db_session,
        _FakeObjectStore(),
        LocalKMS(os.urandom(32)),
        _gateway(db_session, redis_client, test_settings),
        test_settings,
    )


async def test_file_upload_binds_for_file_read_with_no_installations(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_and_user(
        client, "registry-file@example.com"
    )
    tools = await _registry(db_session, redis_client, test_settings).tools_for_run(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=uuid.uuid4(),
        conversation_id=conversation_id,
        capabilities=["file.read"],
    )
    names = {t.llm_name for t in tools}
    assert names == {"file_upload__list_attachments", "file_upload__read_table"}


async def test_no_tools_bind_when_nothing_provides_the_requested_capability(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_and_user(
        client, "registry-none@example.com"
    )
    tools = await _registry(db_session, redis_client, test_settings).tools_for_run(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=uuid.uuid4(),
        conversation_id=conversation_id,
        capabilities=["sql.query"],
    )
    assert len(tools) == 0
    assert bool(tools) is False


async def test_a_healthy_postgres_installation_binds_its_namespaced_tools_with_decrypted_secrets(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_and_user(
        client, "registry-postgres@example.com"
    )
    installations = ConnectorInstallationRepository(db_session)
    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key="postgres",
        name="Sales DB",
        slug="sales-db",
        config={"host": "db", "port": 5432, "database": "sales", "schemas": ["public"]},
        priority=100,
        installed_by=user_id,
    )
    await sync_installation(db_session, LocalKMS(os.urandom(32)), installation)
    await installations.set_health(workspace_id, installation.id, health="healthy", message="ok")
    kms = LocalKMS(os.urandom(32))
    await ConnectorCredentialRepository(db_session).put(
        workspace_id=workspace_id,
        installation_id=installation.id,
        encrypted=encrypt_secrets(kms, {"username": "ro_user", "password": "s3cret"}),
    )

    gateway = _gateway(db_session, redis_client, test_settings)
    registry = ToolRegistry(db_session, _FakeObjectStore(), kms, gateway, test_settings)
    tools = await registry.tools_for_run(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=uuid.uuid4(),
        conversation_id=conversation_id,
        capabilities=["sql.query"],
    )
    names = {t.llm_name for t in tools}
    assert names == {"sales-db__list_tables", "sales-db__describe_table", "sales-db__run_sql"}

    bound = tools.lookup("sales-db__run_sql")
    assert bound is not None
    assert bound.ctx.secrets == {"username": "ro_user", "password": "s3cret"}
    assert bound.installation_id == installation.id

    declarations = tools.to_gemini_declarations()
    run_sql_decl = next(d for d in declarations if d["name"] == "sales-db__run_sql")
    assert run_sql_decl["description"].startswith("[READ]")
    assert run_sql_decl["parameters"]["required"] == ["sql"]


async def test_an_unhealthy_installation_does_not_bind(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_and_user(
        client, "registry-unhealthy@example.com"
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

    tools = await _registry(db_session, redis_client, test_settings).tools_for_run(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=uuid.uuid4(),
        conversation_id=conversation_id,
        capabilities=["sql.query"],
    )
    assert len(tools) == 0


async def test_documents_binds_for_knowledge_search_with_no_installations(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    # Documents is always-available like file_upload (section 10.8): its tools bind on the
    # capability name alone, with no `connector_installations` row involved.
    workspace_id, user_id, conversation_id = await _register_workspace_and_user(
        client, "registry-documents@example.com"
    )
    tools = await _registry(db_session, redis_client, test_settings).tools_for_run(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=uuid.uuid4(),
        conversation_id=conversation_id,
        capabilities=["knowledge.search"],
    )
    names = {t.llm_name for t in tools}
    assert names == {
        "documents__search_documents",
        "documents__get_document",
        "documents__list_collections",
    }


async def _synced_postgres(
    db_session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    slug: str,
    priority: int = 100,
) -> uuid.UUID:
    installations = ConnectorInstallationRepository(db_session)
    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key="postgres",
        name=slug,
        slug=slug,
        config={"host": "db", "port": 5432, "database": "sales"},
        priority=priority,
        installed_by=user_id,
    )
    await sync_installation(db_session, LocalKMS(os.urandom(32)), installation)
    await installations.set_health(workspace_id, installation.id, health="healthy", message="ok")
    return installation.id


async def test_a_disabled_row_is_neither_bound_nor_available(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_and_user(
        client, "registry-disabled-row@example.com"
    )
    installation_id = await _synced_postgres(db_session, workspace_id, user_id, "sales-db")
    tools_repo = ToolDefinitionRepository(db_session)
    for row in await tools_repo.list_for_installation(workspace_id, installation_id):
        if row.name == "run_sql":
            row.is_enabled = False
            # A capability no other row provides, so hiding it from the resolver is observable.
            row.capabilities = ["custom.sql.write"]
    await db_session.flush()

    tools = await _registry(db_session, redis_client, test_settings).tools_for_run(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=uuid.uuid4(),
        conversation_id=conversation_id,
        capabilities=["sql.query", "custom.sql.write"],
    )
    assert {t.llm_name for t in tools} == {"sales-db__list_tables", "sales-db__describe_table"}

    available = await resolve_available_capabilities(
        tools_repo,
        AttachmentRepository(db_session),
        DocumentRepository(db_session),
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    assert "custom.sql.write" not in available


async def test_only_the_best_priority_installation_binds_for_a_shared_capability(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis, test_settings
) -> None:
    workspace_id, user_id, conversation_id = await _register_workspace_and_user(
        client, "registry-priority@example.com"
    )
    await _synced_postgres(db_session, workspace_id, user_id, "backup-db", priority=100)
    primary_id = await _synced_postgres(db_session, workspace_id, user_id, "main-db", priority=1)

    tools = await _registry(db_session, redis_client, test_settings).tools_for_run(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=uuid.uuid4(),
        conversation_id=conversation_id,
        capabilities=["sql.query"],
    )
    assert {t.llm_name for t in tools} == {
        "main-db__list_tables",
        "main-db__describe_table",
        "main-db__run_sql",
    }
    assert {t.installation_id for t in tools} == {primary_id}
