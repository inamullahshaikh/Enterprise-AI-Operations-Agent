"""Keyed by `installation_id` (a 1:1 row, not a surrogate `id`), so — like
`WorkspaceMemberRepository` — this doesn't extend `WorkspaceScopedRepository`, but every
method still takes `workspace_id` explicitly (docs/system-design.md section 14.4)."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.connectors import ConnectorCredential, ConnectorInstallation
from relay_core.security.crypto import EncryptedSecrets


class ConnectorCredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, workspace_id: uuid.UUID, installation_id: uuid.UUID
    ) -> ConnectorCredential | None:
        credential = await self.session.get(ConnectorCredential, installation_id)
        if credential is None or credential.workspace_id != workspace_id:
            return None
        return credential

    async def put(
        self,
        *,
        workspace_id: uuid.UUID,
        installation_id: uuid.UUID,
        encrypted: EncryptedSecrets,
        oauth_expires_at: datetime | None = None,
    ) -> ConnectorCredential:
        """Replaces the row wholesale, so an OAuth caller passes the new expiry alongside the
        new tokens; anything else leaves it null."""
        credential = ConnectorCredential(
            installation_id=installation_id,
            workspace_id=workspace_id,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            encrypted_dek=encrypted.encrypted_dek,
            kms_key_id=encrypted.kms_key_id,
            oauth_expires_at=oauth_expires_at,
        )
        await self.session.merge(credential)
        await self.session.flush()
        return credential

    async def list_expiring_across_workspaces(
        self, before: datetime
    ) -> list[tuple[ConnectorCredential, ConnectorInstallation]]:
        """Every OAuth credential expiring before `before`, with its installation.

        Deliberately cross-tenant, like `ApprovalRepository.list_expired_across_workspaces`: a
        refresh sweep has no workspace to scope to. Named loudly rather than hidden behind an
        optional `workspace_id=None`, and everything it returns is written back through the
        tenant-scoped methods above.
        """
        stmt = (
            select(ConnectorCredential, ConnectorInstallation)
            .join(
                ConnectorInstallation,
                ConnectorCredential.installation_id == ConnectorInstallation.id,
            )
            .where(
                ConnectorCredential.oauth_expires_at.is_not(None),
                ConnectorCredential.oauth_expires_at <= before,
                ConnectorInstallation.status == "active",
            )
            .order_by(ConnectorCredential.oauth_expires_at)
        )
        return [(c, i) for c, i in (await self.session.execute(stmt)).all()]
