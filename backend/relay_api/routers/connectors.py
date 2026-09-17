"""Connector installation routes (docs/system-design.md section 15.3): browse the catalog;
install, inspect, edit (name, priority, status), uninstall and health-check an installation;
re-run its tool discovery (`relay_core.tools.sync`); and preview an OpenAPI spec before installing
the `openapi` connector. Per-tool review and the capability map live in `relay_api.routers.tools`.
`file_upload` never appears here — it's always available rather than admin-installed
(relay_core.capabilities.resolver / relay_core.tools.registry docstrings).
"""

import re
import unicodedata
import uuid
from typing import Any, Literal

import httpx
import jsonschema
from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import (
    CurrentUser,
    get_kms,
    get_llm_gateway,
    get_settings_dep,
    require_workspace_role,
)
from relay_core.config import Settings
from relay_core.connectors.manifest import ConnectorManifest, load_manifests
from relay_core.connectors.openapi_spec import Preview, parse_spec, preview
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.session import get_session
from relay_core.llm.gateway import LLMGateway
from relay_core.security.credential_codec import encrypt_secrets
from relay_core.security.crypto import LocalKMS
from relay_core.security.rbac import Role
from relay_core.security.ssrf import SSRFBlocked, guarded_client, read_capped
from relay_core.tools.sync import (
    SyncReport,
    installation_context,
    installation_secrets,
    sync_installation,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/connectors", tags=["connectors"])
catalog_router = APIRouter(prefix="/connectors", tags=["connectors"])

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Always-available connectors (no installation row, no config/secrets — sections 10.2/10.8):
# excluded from the install catalog and rejected by install_connector below.
_ALWAYS_AVAILABLE_KEYS = frozenset({"file_upload", "documents", "python_sandbox"})


def _slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_name.lower()).strip("-")
    return slug or "connector"


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


class InstallationPatch(BaseModel):
    name: str | None = Field(None, min_length=1)
    priority: int | None = None
    status: Literal["active", "disabled"] | None = None


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
        ManifestOut.from_manifest(m)
        for m in load_manifests().values()
        if m.key not in _ALWAYS_AVAILABLE_KEYS
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
    gateway: LLMGateway = Depends(get_llm_gateway),
    settings: Settings = Depends(get_settings_dep),
) -> InstallationOut:
    manifest = load_manifests().get(body.connector_key)
    if (
        manifest is None
        or body.connector_key not in CONNECTOR_TYPES
        or body.connector_key in _ALWAYS_AVAILABLE_KEYS
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown connector {body.connector_key!r}"
        )

    # Defaults the manifest declares are applied here, where every install passes, so a connector
    # never has to guess at a value the admin left out.
    defaults = {
        k: v["default"]
        for k, v in manifest.config_schema.get("properties", {}).items()
        if "default" in v
    }
    config = {**defaults, **body.config}
    for schema, payload, label in (
        (manifest.config_schema, config, "config"),
        (manifest.secrets_schema, body.secrets, "secrets"),
    ):
        if schema:
            try:
                jsonschema.validate(payload, schema)
            except jsonschema.ValidationError as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"Invalid {label}: {exc.message}"
                ) from exc
    connector = CONNECTOR_TYPES[body.connector_key]()
    try:
        await connector.validate_config(config)
    except (ValueError, SSRFBlocked) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid config: {exc}") from exc

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
        config=config,
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

    ctx = installation_context(workspace_id, current.user.id, installation, body.secrets)
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
    if healthy:
        await sync_installation(session, kms, installation, gateway=gateway, settings=settings)

    return InstallationOut.from_model(installation)


class OpenAPIPreviewRequest(BaseModel):
    spec: str | None = Field(None, max_length=2_000_000)
    spec_url: str | None = None


@router.post("/openapi/preview", response_model=Preview)
async def preview_openapi_spec(
    body: OpenAPIPreviewRequest,
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
) -> Preview:
    """Candidate tools from an OpenAPI 3.x spec (section 6.5 steps 1-4), pasted or fetched. The
    admin installs an `openapi` connector with the operations they pick."""
    if (body.spec is None) == (body.spec_url is None):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pass exactly one of spec or spec_url")
    text = body.spec or ""
    if body.spec_url is not None:
        try:
            async with guarded_client() as http, http.stream("GET", body.spec_url) as resp:
                resp.raise_for_status()
                text = (await read_capped(resp)).decode("utf-8", errors="replace")
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Could not fetch the spec: {exc}"
            ) from exc
    try:
        return preview(parse_spec(text))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/{installation_id}", response_model=InstallationOut)
async def get_installation(
    workspace_id: uuid.UUID = Path(...),
    installation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> InstallationOut:
    installation = await _owned_installation(session, workspace_id, installation_id)
    return InstallationOut.from_model(installation)


@router.patch("/{installation_id}", response_model=InstallationOut)
async def update_installation(
    body: InstallationPatch,
    workspace_id: uuid.UUID = Path(...),
    installation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> InstallationOut:
    """Priority is how an admin picks which installation serves a capability both provide: the
    lowest number wins (section 7.2 rule 2). The slug never changes, so tool names stay stable."""
    installation = await _owned_installation(session, workspace_id, installation_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(installation, field, value)
    await session.flush()
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
        ctx = installation_context(workspace_id, current.user.id, installation, {})
        await connector_cls().on_uninstall(ctx)
    await ConnectorInstallationRepository(session).delete(workspace_id, installation_id)


@router.post("/{installation_id}/test", response_model=InstallationOut)
async def test_connector(
    workspace_id: uuid.UUID = Path(...),
    installation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
    kms: LocalKMS = Depends(get_kms),
    gateway: LLMGateway = Depends(get_llm_gateway),
    settings: Settings = Depends(get_settings_dep),
) -> InstallationOut:
    installation = await _owned_installation(session, workspace_id, installation_id)
    connector_cls = CONNECTOR_TYPES.get(installation.connector_key)
    if connector_cls is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown connector {installation.connector_key!r}"
        )

    secrets = await installation_secrets(session, kms, installation)
    ctx = installation_context(workspace_id, current.user.id, installation, secrets)
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
    if healthy:
        await sync_installation(session, kms, installation, gateway=gateway, settings=settings)
    return InstallationOut.from_model(installation)


@router.post("/{installation_id}/sync", response_model=SyncReport)
async def sync_connector_tools(
    workspace_id: uuid.UUID = Path(...),
    installation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
    kms: LocalKMS = Depends(get_kms),
    gateway: LLMGateway = Depends(get_llm_gateway),
    settings: Settings = Depends(get_settings_dep),
) -> SyncReport:
    installation = await _owned_installation(session, workspace_id, installation_id)
    return await sync_installation(session, kms, installation, gateway=gateway, settings=settings)
