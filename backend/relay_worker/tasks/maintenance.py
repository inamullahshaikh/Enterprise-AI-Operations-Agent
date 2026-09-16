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
import sys

from sqlalchemy.engine import make_url

from relay_core.config import get_settings
from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.builtin.postgres import PostgresConnector
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository, WorkspaceRepository
from relay_core.db.session import get_sessionmaker
from relay_core.security.credential_codec import encrypt_secrets
from relay_core.security.crypto import build_kms
from relay_core.security.passwords import hash_password
from relay_core.security.rbac import Role

_DEMO_EMAIL = "demo@relay.local"
_DEMO_PASSWORD = "relay-demo-only"  # local/dev fixture only — never used outside seed_demo
_DEMO_WORKSPACE_SLUG = "northstar-demo"
_DEMO_INSTALLATION_SLUG = "northstar-db"


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

    print(f"Demo workspace: {_DEMO_WORKSPACE_SLUG} (user: {_DEMO_EMAIL} / {_DEMO_PASSWORD})")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "seed_demo":
        raise SystemExit("Usage: python -m relay_worker.tasks.maintenance seed_demo")
    asyncio.run(seed_demo())
