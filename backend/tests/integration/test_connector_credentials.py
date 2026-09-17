"""`connector_credentials.oauth_expires_at` and the refresh sweep's query (Phase 7 A2,
docs/system-design.md section 18.3 step 6)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceRepository
from relay_core.security.crypto import build_kms

pytestmark = pytest.mark.asyncio


async def _installation(session: AsyncSession, label: str, *, status: str = "active") -> tuple:
    user = await UserRepository(session).create(
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com", full_name=label, password_hash="x"
    )
    await session.flush()
    workspace = await WorkspaceRepository(session).create(
        name=label, slug=f"{label}-{uuid.uuid4().hex[:8]}", created_by=user.id
    )
    installation = await ConnectorInstallationRepository(session).create(
        workspace_id=workspace.id,
        connector_key="gmail",
        name=label,
        slug=label,
        config={"base_url": "https://gmail.googleapis.com"},
        priority=100,
        installed_by=user.id,
    )
    installation.status = status
    await session.flush()
    return workspace.id, installation.id


async def test_the_sweep_finds_only_active_oauth_credentials_expiring_soon(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    kms = build_kms(test_settings)
    repo = ConnectorCredentialRepository(db_session)
    now = datetime.now(UTC)

    soon_ws, soon_id = await _installation(db_session, "soon")
    later_ws, later_id = await _installation(db_session, "later")
    static_ws, static_id = await _installation(db_session, "static")
    off_ws, off_id = await _installation(db_session, "off", status="disabled")

    for ws, installation_id, expires_at in (
        (soon_ws, soon_id, now + timedelta(minutes=5)),
        (later_ws, later_id, now + timedelta(hours=2)),
        (static_ws, static_id, None),  # an API-key connector: no expiry to sweep
        (off_ws, off_id, now + timedelta(minutes=5)),
    ):
        await repo.put(
            workspace_id=ws,
            installation_id=installation_id,
            encrypted=kms.encrypt(b'{"access_token": "t"}'),
            oauth_expires_at=expires_at,
        )

    expiring = await repo.list_expiring_across_workspaces(now + timedelta(minutes=15))

    assert [installation.id for _, installation in expiring] == [soon_id]
    stored = await repo.get(soon_ws, soon_id)
    assert stored is not None and stored.oauth_expires_at is not None
