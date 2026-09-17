"""Connector installations & credentials (docs/system-design.md section 14.3), trimmed per
docs/adr/0009-phase3-connector-metadata-in-code.md: no `connector_definitions` FK (the
manifest registry in `relay_core.connectors.manifest` is the catalog), no `capability_bindings`
table (`priority` lives directly on the installation), no `allowed_roles`/`openapi_spec_key`.
`last_synced_at` arrives in Phase 6 with tool discovery (`relay_core.db.models.tools`).

`file_upload` never gets a row here — see `relay_core.capabilities.resolver` and
`relay_core.tools.registry` for why it's always available instead of admin-installed.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from relay_core.db.base import Base, TimestampMixin, WorkspaceScoped, uuid7


class ConnectorInstallation(Base, WorkspaceScoped, TimestampMixin):
    __tablename__ = "connector_installations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    connector_key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    health: Mapped[str] = mapped_column(
        String, nullable=False, default="unknown", server_default="unknown"
    )
    health_message: Mapped[str | None] = mapped_column(Text)
    last_health_at: Mapped[datetime | None]
    last_synced_at: Mapped[datetime | None]
    priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=100, server_default="100"
    )
    installed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_installations_ws_slug"),
        CheckConstraint(
            "status IN ('pending','active','disabled','error')", name="ck_installations_status"
        ),
        CheckConstraint(
            "health IN ('unknown','healthy','degraded','down')", name="ck_installations_health"
        ),
    )


class ConnectorCredential(Base, TimestampMixin):
    """1:1 with an installation. `nonce`/`encrypted_dek`/`kms_key_id` map directly onto
    `relay_core.security.crypto.EncryptedSecrets` — see that module for the envelope
    encryption scheme."""

    __tablename__ = "connector_credentials"

    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connector_installations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kms_key_id: Mapped[str] = mapped_column(String, nullable=False)
