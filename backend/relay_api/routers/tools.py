"""Tool review and the capability map (docs/system-design.md section 15.3, FR-7, FR-8).

Admins review what discovery found: enable or disable a tool, override its risk, assign its
capabilities, and clear the review flag. The capability map shows which installation serves each
capability (the priority "winner"), who else could, and which taxonomy capabilities nothing
provides. Priorities themselves are edited on the installation
(`PATCH /workspaces/{ws}/connectors/{id}`).

Edits apply to the next tool binding. A run parked on an approval is safe across them:
`approval_gate` matches approved rows to calls by the approval's own proposed arguments, not by
re-reading risk.
"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import CurrentUser, require_workspace_role
from relay_core.capabilities.resolver import always_available
from relay_core.capabilities.tagger import valid_capability
from relay_core.capabilities.taxonomy import CAPABILITY_TAXONOMY
from relay_core.connectors.manifest import load_manifests
from relay_core.db.models.tools import ToolDefinition
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.tool_definitions import HEALTHY_ENOUGH, ToolDefinitionRepository
from relay_core.db.session import get_session
from relay_core.security.rbac import Role

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["tools"])

RiskName = Literal["read", "write", "destructive"]


class ToolOut(BaseModel):
    id: uuid.UUID
    installation_id: uuid.UUID
    name: str
    llm_name: str
    description: str
    input_schema: dict[str, Any]
    risk: str
    risk_overridden: bool
    capabilities: list[str]
    capability_source: str
    tag_confidence: float | None
    idempotent: bool
    timeout_s: float
    is_enabled: bool
    needs_review: bool

    @classmethod
    def from_model(cls, t: ToolDefinition) -> "ToolOut":
        return cls.model_validate(t, from_attributes=True)


class ToolPatch(BaseModel):
    is_enabled: bool | None = None
    risk: RiskName | None = None
    capabilities: list[str] | None = None
    reviewed: Literal[True] | None = None


class ProviderOut(BaseModel):
    installation_id: uuid.UUID | None
    name: str
    slug: str
    priority: int | None
    health: str
    tool_count: int | None
    winner: bool


class CapabilityOut(BaseModel):
    capability: str
    available: bool
    providers: list[ProviderOut] = Field(default_factory=list)


class CapabilityMap(BaseModel):
    capabilities: list[CapabilityOut]
    gaps: list[str]


@router.get("/tools", response_model=list[ToolOut])
async def list_tools(
    workspace_id: uuid.UUID = Path(...),
    capability: str | None = Query(None),
    risk: RiskName | None = Query(None),
    needs_review: bool | None = Query(None),
    installation_id: uuid.UUID | None = Query(None),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> list[ToolOut]:
    tools = await ToolDefinitionRepository(session).list_for_workspace(
        workspace_id,
        capability=capability,
        risk=risk,
        needs_review=needs_review,
        installation_id=installation_id,
    )
    return [ToolOut.from_model(t) for t in tools]


@router.patch("/tools/{tool_id}", response_model=ToolOut)
async def update_tool(
    body: ToolPatch,
    workspace_id: uuid.UUID = Path(...),
    tool_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> ToolOut:
    tool = await ToolDefinitionRepository(session).get(workspace_id, tool_id)
    if tool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tool not found")
    if body.capabilities is not None:
        invalid = [c for c in body.capabilities if not valid_capability(c)]
        if invalid:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Not a taxonomy key or custom.<domain>.<action>: {', '.join(invalid)}",
            )
        tool.capabilities = list(dict.fromkeys(body.capabilities))
        tool.capability_source = "admin"
        tool.embedding = None  # the embedded text names the capabilities; the next sync redoes it
    if body.is_enabled is not None:
        tool.is_enabled = body.is_enabled
    if body.risk is not None:
        tool.risk, tool.risk_overridden = body.risk, True
    if body.reviewed:
        tool.needs_review = False
    await session.flush()
    return ToolOut.from_model(tool)


@router.get("/capabilities", response_model=CapabilityMap)
async def capability_map(
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> CapabilityMap:
    taxonomy = [c.key for c in CAPABILITY_TAXONOMY]
    providers: dict[str, dict[uuid.UUID | None, ProviderOut]] = {key: {} for key in taxonomy}
    manifests = load_manifests()
    for capability, key in (
        await always_available(DocumentRepository(session), workspace_id)
    ).items():
        providers.setdefault(capability, {})[None] = ProviderOut(
            installation_id=None,
            name=manifests[key].display_name,
            slug=key,
            priority=None,
            health="healthy",
            tool_count=None,
            winner=True,
        )

    # Rows arrive best priority first, so the first healthy installation seen per capability wins.
    # Always-available sources bind alongside it rather than competing, so they don't count.
    for row, installation in await ToolDefinitionRepository(session).list_enabled(workspace_id):
        for capability in row.capabilities:
            by_installation = providers.setdefault(capability, {})
            provider = by_installation.get(installation.id)
            if provider is None:
                bindable = installation.status == "active" and installation.health in HEALTHY_ENOUGH
                provider = by_installation[installation.id] = ProviderOut(
                    installation_id=installation.id,
                    name=installation.name,
                    slug=installation.slug,
                    priority=installation.priority,
                    health=installation.health,
                    tool_count=0,
                    winner=bindable
                    and not any(p.winner and p.installation_id for p in by_installation.values()),
                )
            provider.tool_count = (provider.tool_count or 0) + 1

    capabilities = [
        CapabilityOut(
            capability=capability,
            available=any(p.winner for p in by_installation.values()),
            providers=list(by_installation.values()),
        )
        for capability, by_installation in providers.items()
    ]
    gaps = [c.capability for c in capabilities if not c.available and c.capability in taxonomy]
    return CapabilityMap(capabilities=capabilities, gaps=gaps)
