"""Installs a built-in connector without the HTTP install route, for the two bootstraps that
have no admin making the request: `relay_worker.tasks.maintenance.seed_demo` and the eval
harness's per-profile workspaces (`evals/relay_eval`). Idempotent by slug, so re-running either
leaves an existing installation alone.

Validation is deliberately skipped: both callers pass fixed, known-good config, whereas
`relay_api.routers.connectors.install_connector` is where untrusted config gets checked against
the manifest.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.security.credential_codec import encrypt_secrets
from relay_core.security.crypto import LocalKMS


async def ensure_installation(
    session: AsyncSession,
    kms: LocalKMS,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    connector_key: str,
    name: str,
    slug: str,
    config: dict[str, Any],
    secrets: dict[str, str] | None = None,
) -> None:
    installations = ConnectorInstallationRepository(session)
    if await installations.get_by_slug(workspace_id, slug) is not None:
        return

    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key=connector_key,
        name=name,
        slug=slug,
        config=config,
        priority=100,
        installed_by=user_id,
    )
    if secrets:
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
        secrets=secrets or {},
    )
    healthy, message = await CONNECTOR_TYPES[connector_key]().health_check(ctx)
    await installations.set_health(
        workspace_id,
        installation.id,
        health="healthy" if healthy else "down",
        message=message,
        status="active" if healthy else "error",
    )
