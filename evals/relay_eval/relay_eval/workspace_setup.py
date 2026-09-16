"""Per-connector-profile workspace provisioning for the eval harness (docs/system-design.md
section 21.2), trimmed to the profiles Phase 3's two connector types can actually produce:
`db_only` (postgres against the demo DB), `csv_only` (a fixture CSV attached, no connectors),
and `none` (nothing). `full`/`crm_only`/`docs_only` wait for the connectors that would give
them distinct meaning (Gmail/Calendar/HubSpot/documents — Phases 4-7).

Each profile gets its own fixed, idempotently-created workspace (`eval-<profile>`) reused
across harness runs, mirroring `relay_worker.tasks.maintenance.seed_demo`'s pattern — cases
get a fresh conversation each, but re-provisioning a whole workspace and connector per case
would make every run slower for no benefit.
"""

import uuid
from pathlib import Path

from relay_core.config import Settings
from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.builtin.csv_profile import infer_capabilities, profile_csv
from relay_core.connectors.builtin.postgres import PostgresConnector
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository, WorkspaceRepository
from relay_core.security.credential_codec import encrypt_secrets
from relay_core.security.crypto import LocalKMS
from relay_core.security.passwords import hash_password
from relay_core.security.rbac import Role
from relay_core.storage.object_store import ObjectStore
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

_EVAL_EMAIL = "eval-harness@relay.local"
_INSTALLATION_SLUG = "eval-demo-db"


async def ensure_eval_user(session: AsyncSession) -> uuid.UUID:
    users = UserRepository(session)
    user = await users.get_by_email(_EVAL_EMAIL)
    if user is None:
        user = await users.create(
            email=_EVAL_EMAIL,
            full_name="Eval harness",
            password_hash=hash_password(uuid.uuid4().hex),
            email_verified=True,
        )
        await session.flush()
    return user.id


async def ensure_workspace_for_profile(
    session: AsyncSession, settings: Settings, kms: LocalKMS, user_id: uuid.UUID, profile: str
) -> uuid.UUID:
    slug = f"eval-{profile.replace('_', '-')}"
    workspaces = WorkspaceRepository(session)
    workspace = await workspaces.get_by_slug(slug)
    if workspace is None:
        workspace = await workspaces.create(name=f"Eval: {profile}", slug=slug, created_by=user_id)
        await WorkspaceMemberRepository(session).add(
            workspace_id=workspace.id, user_id=user_id, role=Role.owner.name
        )
        await session.flush()

    if profile == "db_only":
        await _ensure_postgres_installation(session, settings, kms, user_id, workspace.id)

    return workspace.id


async def _ensure_postgres_installation(
    session: AsyncSession,
    settings: Settings,
    kms: LocalKMS,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    if not settings.demo_db_url:
        raise RuntimeError("DEMO_DB_URL is not set — the db_only eval profile needs it")
    installations = ConnectorInstallationRepository(session)
    if await installations.get_by_slug(workspace_id, _INSTALLATION_SLUG) is not None:
        return

    url = make_url(settings.demo_db_url)
    config = {
        "host": url.host,
        "port": url.port or 5432,
        "database": url.database,
        "schemas": ["public"],
        "statement_timeout_s": 10,
        "row_limit": 500,
    }
    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key="postgres",
        name="Eval demo DB",
        slug=_INSTALLATION_SLUG,
        config=config,
        priority=100,
        installed_by=user_id,
    )
    secrets = {"username": url.username or "", "password": url.password or ""}
    await ConnectorCredentialRepository(session).put(
        workspace_id=workspace_id,
        installation_id=installation.id,
        encrypted=encrypt_secrets(kms, secrets),
    )
    await session.flush()

    ctx = ExecutionContext(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=installation.id,
        conversation_id=installation.id,
        installation_id=str(installation.id),
        config=config,
        secrets=secrets,
    )
    healthy, message = await PostgresConnector().health_check(ctx)
    await installations.set_health(
        workspace_id,
        installation.id,
        health="healthy" if healthy else "down",
        message=message,
        status="active" if healthy else "error",
    )


async def new_conversation(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    conversation = await ConversationRepository(session).create(
        workspace_id=workspace_id, user_id=user_id
    )
    await session.flush()
    return conversation.id


async def attach_csv_fixture(
    session: AsyncSession,
    object_store: ObjectStore,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    fixtures_dir: Path,
    fixture_name: str,
) -> None:
    raw = (fixtures_dir / fixture_name).read_bytes()
    profile = profile_csv(raw)
    blob_key = f"eval-attachments/{workspace_id}/{uuid.uuid4()}/{fixture_name}"
    await object_store.put_bytes(blob_key, raw, content_type="text/csv")
    await AttachmentRepository(session).create(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        filename=fixture_name,
        mime_type="text/csv",
        size_bytes=len(raw),
        blob_key=blob_key,
        kind="table",
        profile=profile.to_json(),
        inferred_capabilities=infer_capabilities(profile.columns),
        uploaded_by=user_id,
    )
    await session.flush()
