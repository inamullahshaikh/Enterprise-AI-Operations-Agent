"""Keyed by `installation_id` (a 1:1 row, not a surrogate `id`), so — like
`WorkspaceMemberRepository` — this doesn't extend `WorkspaceScopedRepository`, but every
method still takes `workspace_id` explicitly (docs/system-design.md section 14.4)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.connectors import ConnectorCredential
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
        self, *, workspace_id: uuid.UUID, installation_id: uuid.UUID, encrypted: EncryptedSecrets
    ) -> ConnectorCredential:
        credential = ConnectorCredential(
            installation_id=installation_id,
            workspace_id=workspace_id,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            encrypted_dek=encrypted.encrypted_dek,
            kms_key_id=encrypted.kms_key_id,
        )
        await self.session.merge(credential)
        await self.session.flush()
        return credential
