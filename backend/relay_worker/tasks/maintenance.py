"""One-time local/dev bootstrap: creates a demo workspace and installs a `postgres` connector
pointing at the seeded `demo-db` service (docs/system-design.md section 27), so `make seed`
leaves a ready-to-demo workspace instead of an empty one. Idempotent — safe to run again.

Run directly (`python -m relay_worker.tasks.maintenance seed_demo`), not as a Celery task:
this only ever needs to run once per environment, by a human, not on a schedule or in response
to an event, so it doesn't need the task queue's retry/routing machinery. Later Phase 8
maintenance jobs (retention, budget resets) that genuinely are scheduled register as real
Celery tasks in this same module instead.
"""

import asyncio
import hashlib
import sys
import uuid
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings, get_settings
from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.builtin.postgres import PostgresConnector
from relay_core.db.repositories.collections import CollectionRepository
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository, WorkspaceRepository
from relay_core.db.session import get_sessionmaker
from relay_core.llm.gateway import LLMGateway, build_llm_gateway
from relay_core.rag.ingest import ingest_document
from relay_core.security.credential_codec import encrypt_secrets
from relay_core.security.crypto import build_kms
from relay_core.security.passwords import hash_password
from relay_core.security.rbac import Role
from relay_core.storage.object_store import ObjectStore, build_object_store

_DEMO_EMAIL = "demo@relay.local"
_DEMO_PASSWORD = "relay-demo-only"  # local/dev fixture only — never used outside seed_demo
_DEMO_WORKSPACE_SLUG = "northstar-demo"
_DEMO_INSTALLATION_SLUG = "northstar-db"

# Mounted read-only into the api/worker containers by docker-compose.yml — see that file's
# comment on the api service's volumes for why only this one demo/ subdirectory is mounted.
_DEMO_DOCUMENTS_DIR = Path("/demo/documents")
_DEMO_DOCUMENTS = [
    ("renewal-playbook.md", "Renewal Playbook"),
    ("support-escalation-policy.md", "Support Escalation Policy"),
]


async def seed_demo() -> None:
    settings = get_settings()
    if not settings.demo_db_url:
        raise SystemExit("DEMO_DB_URL is not set — nothing to point the demo connector at")
    url = make_url(settings.demo_db_url)
    kms = build_kms(settings)

    async with get_sessionmaker()() as session:
        users = UserRepository(session)
        user = await users.get_by_email(_DEMO_EMAIL)
        if user is None:
            user = await users.create(
                email=_DEMO_EMAIL,
                full_name="Demo",
                password_hash=hash_password(_DEMO_PASSWORD),
                email_verified=True,
            )

        workspaces = WorkspaceRepository(session)
        workspace = await workspaces.get_by_slug(_DEMO_WORKSPACE_SLUG)
        if workspace is None:
            workspace = await workspaces.create(
                name="Northstar Analytics (demo)", slug=_DEMO_WORKSPACE_SLUG, created_by=user.id
            )
            await WorkspaceMemberRepository(session).add(
                workspace_id=workspace.id, user_id=user.id, role=Role.owner.name
            )

        installations = ConnectorInstallationRepository(session)
        installation = await installations.get_by_slug(workspace.id, _DEMO_INSTALLATION_SLUG)
        if installation is None:
            config = {
                "host": url.host,
                "port": url.port or 5432,
                "database": url.database,
                "schemas": ["public"],
                "statement_timeout_s": 10,
                "row_limit": 500,
            }
            installation = await installations.create(
                workspace_id=workspace.id,
                connector_key="postgres",
                name="Northstar demo DB",
                slug=_DEMO_INSTALLATION_SLUG,
                config=config,
                priority=100,
                installed_by=user.id,
            )
            secrets = {"username": url.username or "", "password": url.password or ""}
            await ConnectorCredentialRepository(session).put(
                workspace_id=workspace.id,
                installation_id=installation.id,
                encrypted=encrypt_secrets(kms, secrets),
            )
            await session.flush()

            ctx = ExecutionContext(
                workspace_id=workspace.id,
                user_id=user.id,
                run_id=installation.id,
                conversation_id=installation.id,
                installation_id=str(installation.id),
                config=config,
                secrets=secrets,
            )
            healthy, message = await PostgresConnector().health_check(ctx)
            await installations.set_health(
                workspace.id,
                installation.id,
                health="healthy" if healthy else "down",
                message=message,
                status="active" if healthy else "error",
            )

        await session.commit()

        redis = Redis.from_url(settings.redis_url)
        try:
            gateway = build_llm_gateway(session, redis, settings)
            object_store = build_object_store(settings)
            await _seed_demo_documents(
                session, workspace.id, user.id, settings, gateway, object_store
            )
            await session.commit()
        finally:
            await redis.aclose()

    print(f"Demo workspace: {_DEMO_WORKSPACE_SLUG} (user: {_DEMO_EMAIL} / {_DEMO_PASSWORD})")


async def _seed_demo_documents(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    settings: Settings,
    gateway: LLMGateway,
    object_store: ObjectStore,
) -> None:
    """Ingests demo/documents/*.md into the demo workspace's knowledge base — idempotent via
    the same sha256-dedup `relay_api/routers/documents.py`'s upload endpoint uses, so re-running
    `make seed` doesn't re-ingest (and re-embed, at real Gemini API cost) unchanged content.
    """
    if not _DEMO_DOCUMENTS_DIR.is_dir():
        print(f"  (skipping demo documents — {_DEMO_DOCUMENTS_DIR} not mounted)")
        return

    collection = await CollectionRepository(session).get_or_create_default(workspace_id)
    documents = DocumentRepository(session)
    for filename, title in _DEMO_DOCUMENTS:
        path = _DEMO_DOCUMENTS_DIR / filename
        if not path.is_file():
            continue
        raw = path.read_bytes()
        sha256 = hashlib.sha256(raw).hexdigest()
        if await documents.get_by_sha256(workspace_id, collection.id, sha256) is not None:
            continue

        blob_key = f"documents/{workspace_id}/{uuid.uuid4()}/{filename}"
        await object_store.put_bytes(blob_key, raw, content_type="text/markdown")
        document = await documents.create(
            workspace_id=workspace_id,
            collection_id=collection.id,
            title=title,
            blob_key=blob_key,
            mime_type="text/markdown",
            size_bytes=len(raw),
            sha256=sha256,
            uploaded_by=user_id,
        )
        await session.flush()
        await ingest_document(
            workspace_id=workspace_id,
            document_id=document.id,
            session=session,
            object_store=object_store,
            gateway=gateway,
            settings=settings,
        )
        print(f"  ingested demo document: {title}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "seed_demo":
        raise SystemExit("Usage: python -m relay_worker.tasks.maintenance seed_demo")
    asyncio.run(seed_demo())
