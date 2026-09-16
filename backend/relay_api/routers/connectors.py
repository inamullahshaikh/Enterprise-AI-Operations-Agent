"""Connector installation routes (docs/system-design.md section 15.3), trimmed to what
Phase 3 needs: browse the catalog, install/inspect/uninstall/health-check a built-in
connector. Tool listing/enable-disable, OpenAPI/MCP endpoints, and capability-priority editing
are Phase 6 (docs/adr/0009). `file_upload` never appears here — it's always available rather
than admin-installed (relay_core.capabilities.resolver / relay_core.tools.registry docstrings).
"""

import re
import unicodedata
import uuid
from typing import Any

import jsonschema
from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import CurrentUser, get_kms, require_workspace_role
from relay_core.connectors.base import ExecutionContext
from relay_core.connectors.manifest import ConnectorManifest, load_manifests
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.session import get_session
from relay_core.security.credential_codec import decrypt_secrets, encrypt_secrets
from relay_core.security.crypto import LocalKMS
from relay_core.security.rbac import Role

router = APIRouter(prefix="/workspaces/{workspace_id}/connectors", tags=["connectors"])
catalog_router = APIRouter(prefix="/connectors", tags=["connectors"])

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_name.lower()).strip("-")
    return slug or "connector"


def _install_time_context(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    installation: ConnectorInstallation,
    secrets: dict[str, str],
) -> ExecutionContext:
    # There's no real run/conversation at install time (on_install/health_check happen outside
    # any agent run) — the installation's own id fills those two required fields since neither
    # built-in connector ever reads them.
    return ExecutionContext(
        workspace_id=workspace_id,
        user_id=user_id,
        run_id=installation.id,
        conversation_id=installation.id,
        installation_id=str(installation.id),
        config=installation.config,
        secrets=secrets,
    )


class ManifestOut(BaseModel):
    key: str
    display_name: str
    category: str
    description: str
    auth_type: str
    config_schema: dict[str, Any]
    secrets_schema: dict[str, Any]
    provides_capabilities: list[str]

    @classmethod
    def from_manifest(cls, m: ConnectorManifest) -> "ManifestOut":
        return cls(
            key=m.key,
            display_name=m.display_name,
            category=m.category,
            description=m.description,
            auth_type=m.auth_type.value,
            config_schema=m.config_schema,
            secrets_schema=m.secrets_schema,
            provides_capabilities=m.provides_capabilities,
        )


class InstallConnectorRequest(BaseModel):
    connector_key: str
    name: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    priority: int = 100


class InstallationOut(BaseModel):
    id: uuid.UUID
    connector_key: str
    name: str
    slug: str
    config: dict[str, Any]
    status: str
    health: str
    health_message: str | None
    priority: int

    @classmethod
    def from_model(cls, i: ConnectorInstallation) -> "InstallationOut":
        return cls(
            id=i.id,
            connector_key=i.connector_key,
            name=i.name,
            slug=i.slug,
            config=i.config,
            status=i.status,
            health=i.health,
            health_message=i.health_message,
            priority=i.priority,
        )


async def _owned_installation(
    session: AsyncSession, workspace_id: uuid.UUID, installation_id: uuid.UUID
) -> ConnectorInstallation:
    installation = await ConnectorInstallationRepository(session).get(workspace_id, installation_id)
    if installation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connector installation not found")
    return installation


@catalog_router.get("/catalog", response_model=list[ManifestOut])
async def get_catalog() -> list[ManifestOut]:
    return [
        ManifestOut.from_manifest(m) for m in load_manifests().values() if m.key != "file_upload"
    ]


@router.get("", response_model=list[InstallationOut])
async def list_installations(
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> list[InstallationOut]:
    installations = await ConnectorInstallationRepository(session).list_for_workspace(workspace_id)
    return [InstallationOut.from_model(i) for i in installations]


@router.post("", response_model=InstallationOut, status_code=status.HTTP_201_CREATED)
async def install_connector(
    body: InstallConnectorRequest,
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
    kms: LocalKMS = Depends(get_kms),
) -> InstallationOut:
    manifest = load_manifests().get(body.connector_key)
    if (
        manifest is None
        or body.connector_key not in CONNECTOR_TYPES
        or body.connector_key == "file_upload"
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown connector {body.connector_key!r}"
        )

    for schema, payload, label in (
        (manifest.config_schema, body.config, "config"),
        (manifest.secrets_schema, body.secrets, "secrets"),
    ):
        if schema:
            try:
                jsonschema.validate(payload, schema)
            except jsonschema.ValidationError as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"Invalid {label}: {exc.message}"
                ) from exc

    installations = ConnectorInstallationRepository(session)
    base_slug = _slugify(body.name)
    slug = base_slug
    suffix = 1
    while await installations.get_by_slug(workspace_id, slug) is not None:
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    installation = await installations.create(
        workspace_id=workspace_id,
        connector_key=body.connector_key,
        name=body.name,
        slug=slug,
        config=body.config,
        priority=body.priority,
        installed_by=current.user.id,
    )
    if body.secrets:
        await ConnectorCredentialRepository(session).put(
            workspace_id=workspace_id,
            installation_id=installation.id,
            encrypted=encrypt_secrets(kms, body.secrets),
        )
    await session.flush()

    connector = CONNECTOR_TYPES[body.connector_key]()
    ctx = _install_time_context(workspace_id, current.user.id, installation, body.secrets)
    try:
        await connector.on_install(ctx)
        healthy, message = await connector.health_check(ctx)
    except Exception as exc:  # noqa: BLE001 - a bad install must record why, not 500
        healthy, message = False, f"Health check raised: {exc}"
    await installations.set_health(
        workspace_id,
        installation.id,
        health="healthy" if healthy else "down",
        message=message,
        status="active" if healthy else "error",
    )

    return InstallationOut.from_model(installation)


@router.get("/{installation_id}", response_model=InstallationOut)
async def get_installation(
    workspace_id: uuid.UUID = Path(...),
    installation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> InstallationOut:
    installation = await _owned_installation(session, workspace_id, installation_id)
    return InstallationOut.from_model(installation)


@router.delete("/{installation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def uninstall_connector(
    workspace_id: uuid.UUID = Path(...),
    installation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> None:
    installation = await _owned_installation(session, workspace_id, installation_id)
    connector_cls = CONNECTOR_TYPES.get(installation.connector_key)
    if connector_cls is not None:
        ctx = _install_time_context(workspace_id, current.user.id, installation, {})
        await connector_cls().on_uninstall(ctx)
    await ConnectorInstallationRepository(session).delete(workspace_id, installation_id)


@router.post("/{installation_id}/test", response_model=InstallationOut)
async def test_connector(
    workspace_id: uuid.UUID = Path(...),
    installation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
    kms: LocalKMS = Depends(get_kms),
) -> InstallationOut:
    installation = await _owned_installation(session, workspace_id, installation_id)
    connector_cls = CONNECTOR_TYPES.get(installation.connector_key)
    if connector_cls is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown connector {installation.connector_key!r}"
        )

    secrets: dict[str, str] = {}
    credential = await ConnectorCredentialRepository(session).get(workspace_id, installation_id)
    if credential is not None:
        secrets = decrypt_secrets(kms, credential)

    ctx = _install_time_context(workspace_id, current.user.id, installation, secrets)
    try:
        healthy, message = await connector_cls().health_check(ctx)
    except Exception as exc:  # noqa: BLE001 - a failed test must record why, not 500
        healthy, message = False, f"Health check raised: {exc}"

    await ConnectorInstallationRepository(session).set_health(
        workspace_id,
        installation_id,
        health="healthy" if healthy else "down",
        message=message,
        status="active" if healthy else "error",
    )
    return InstallationOut.from_model(installation)
