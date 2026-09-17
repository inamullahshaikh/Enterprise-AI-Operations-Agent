"""Per-connector-profile workspace provisioning for the eval harness (docs/system-design.md
section 21.2), trimmed to the profiles the connectors built so far can actually produce:
`db_only` (postgres against the demo DB), `csv_only` (a fixture CSV attached, no connectors),
`docs_only` (fixture Markdown files ingested into the knowledge base), and `none` (nothing).
`full`/`crm_only` wait for connectors that would give them distinct meaning
(Gmail/Calendar/HubSpot — Phase 5+).

Each profile gets its own fixed, idempotently-created workspace (`eval-<profile>`) reused
across harness runs, mirroring `relay_worker.tasks.maintenance.seed_demo`'s pattern — cases
get a fresh conversation each, but re-provisioning a whole workspace and connector per case
would make every run slower for no benefit.
"""

import hashlib
import uuid
from pathlib import Path

from redis.asyncio import Redis
from relay_core.config import Settings
from relay_core.connectors.builtin.csv_profile import infer_capabilities, profile_csv
from relay_core.connectors.installs import ensure_installation
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.collections import CollectionRepository
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository, WorkspaceRepository
from relay_core.llm.gateway import build_llm_gateway
from relay_core.rag.ingest import ingest_document
from relay_core.security.crypto import LocalKMS
from relay_core.security.passwords import hash_password
from relay_core.security.rbac import Role
from relay_core.storage.object_store import ObjectStore, build_object_store
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

_EVAL_EMAIL = "eval-harness@relay.local"
_INSTALLATION_SLUG = "eval-demo-db"
_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"
_DOCS_ONLY_FIXTURES = [
    ("renewal-playbook.md", "Renewal Playbook"),
    ("support-escalation-policy.md", "Support Escalation Policy"),
]


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
    session: AsyncSession,
    settings: Settings,
    kms: LocalKMS,
    redis: Redis,
    user_id: uuid.UUID,
    profile: str,
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

    if profile in ("db_only", "full"):
        await _ensure_postgres_installation(session, settings, kms, user_id, workspace.id)
    if profile == "full":
        await _ensure_mock_installations(session, settings, kms, user_id, workspace.id)
    elif profile == "docs_only":
        await _ensure_documents_ingested(session, settings, redis, user_id, workspace.id)

    return workspace.id


async def _ensure_postgres_installation(
    session: AsyncSession,
    settings: Settings,
    kms: LocalKMS,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    if not settings.demo_db_url:
        raise RuntimeError("DEMO_DB_URL is not set — the db_only/full eval profiles need it")
    url = make_url(settings.demo_db_url)
    await ensure_installation(
        session,
        kms,
        workspace_id=workspace_id,
        user_id=user_id,
        connector_key="postgres",
        name="Eval demo DB",
        slug=_INSTALLATION_SLUG,
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


async def _ensure_mock_installations(
    session: AsyncSession,
    settings: Settings,
    kms: LocalKMS,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    for connector_key in ("gmail", "google_calendar"):
        await ensure_installation(
            session,
            kms,
            workspace_id=workspace_id,
            user_id=user_id,
            connector_key=connector_key,
            name=f"Eval {connector_key} (mock)",
            slug=f"eval-{connector_key.replace('_', '-')}",
            config={"base_url": settings.mock_services_url},
        )


async def _ensure_documents_ingested(
    session: AsyncSession,
    settings: Settings,
    redis: Redis,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Ingests the `rag` suite's fixture documents into the `docs_only` eval workspace's
    knowledge base — same idempotent-by-sha256 pattern as
    `relay_worker.tasks.maintenance.seed_demo`'s demo-document seeding, and the same reason:
    re-running the harness shouldn't re-embed unchanged content at real Gemini API cost.
    """
    object_store = build_object_store(settings)
    gateway = build_llm_gateway(session, redis, settings)
    collection = await CollectionRepository(session).get_or_create_default(workspace_id)
    documents = DocumentRepository(session)

    for filename, title in _DOCS_ONLY_FIXTURES:
        raw = (_FIXTURES_DIR / filename).read_bytes()
        sha256 = hashlib.sha256(raw).hexdigest()
        if await documents.get_by_sha256(workspace_id, collection.id, sha256) is not None:
            continue

        blob_key = f"eval-documents/{workspace_id}/{uuid.uuid4()}/{filename}"
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
