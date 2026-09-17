"""Maintenance jobs, plus the one-time local/dev bootstrap.

`seed_demo` creates a demo workspace and installs a `postgres` connector pointing at the seeded
`demo-db` service (docs/system-design.md section 27), plus `gmail` and `google_calendar` pointing
at the mock service, so `make seed` leaves a ready-to-demo workspace instead of an empty one.
Idempotent — safe to run again. It runs directly (`python -m relay_worker.tasks.maintenance
seed_demo`), not as a Celery task: it only ever needs to run once per environment, by a human,
not on a schedule.

`expire_stale_approvals` is the first genuinely scheduled job here (Phase 5, section 13.4). An
undecided approval must not pin a run open forever, so once its window has elapsed the approval,
its proposed calls and the run itself are all closed out as expired. Phase 8's retention and
budget-reset jobs register alongside it.
"""

import asyncio
import hashlib
import sys
import uuid
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.approvals import expire_stale_approvals as expire_stale_approvals_once
from relay_core.config import Settings, get_settings
from relay_core.connectors.installs import ensure_installation
from relay_core.db.repositories.collections import CollectionRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository, WorkspaceRepository
from relay_core.db.session import get_sessionmaker
from relay_core.events.publisher import EventPublisher
from relay_core.llm.gateway import LLMGateway, build_llm_gateway
from relay_core.rag.ingest import ingest_document
from relay_core.security.crypto import build_kms
from relay_core.security.passwords import hash_password
from relay_core.security.rbac import Role
from relay_core.storage.object_store import ObjectStore, build_object_store
from relay_worker.app import app

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

        await ensure_installation(
            session,
            kms,
            workspace_id=workspace.id,
            user_id=user.id,
            connector_key="postgres",
            name="Northstar demo DB",
            slug=_DEMO_INSTALLATION_SLUG,
            config={
                "host": url.host,
                "port": url.port or 5432,
                "database": url.database,
                "schemas": ["public"],
                "statement_timeout_s": 10,
                "row_limit": 500,
            },
            secrets={"username": url.username or "", "password": url.password or ""},
        )
        # Phase 5's renewal scenario (section 28) ends in drafted emails, so the demo workspace
        # needs somewhere to draft them: both connectors point at the mock service.
        for connector_key, name in (
            ("gmail", "Gmail (mock)"),
            ("google_calendar", "Calendar (mock)"),
        ):
            await ensure_installation(
                session,
                kms,
                workspace_id=workspace.id,
                user_id=user.id,
                connector_key=connector_key,
                name=name,
                slug=f"northstar-{connector_key.replace('_', '-')}",
                config={"base_url": settings.mock_services_url},
                # Both connectors speak the real Google APIs (Phase 7 B1/B2) and the mock
                # demands a bearer token exactly as Google does. A demo installation pointed at
                # a real Google account gets its token from the OAuth flow instead.
                secrets={"access_token": "demo-mock-token"},
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


@app.task(name="relay_worker.tasks.maintenance.expire_stale_approvals")  # type: ignore[untyped-decorator]
def expire_stale_approvals() -> None:
    asyncio.run(_expire_stale_approvals_async())


async def _expire_stale_approvals_async() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    try:
        async with get_sessionmaker()() as session:
            await expire_stale_approvals_once(session, EventPublisher(redis))
            await session.commit()
    finally:
        await redis.aclose()


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "seed_demo":
        raise SystemExit("Usage: python -m relay_worker.tasks.maintenance seed_demo")
    asyncio.run(seed_demo())
