"""Workspace & membership routes (docs/system-design.md section 15.2).

Invites are simplified for Phase 1: `POST .../members` adds an *existing* user by
email straight into the workspace. A token-based email-invite flow for people
without an account yet is a UI/notifications feature, not foundational plumbing,
so it's deferred rather than half-built here.
"""

import re
import unicodedata
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import CurrentUser, get_current_user, get_redis, require_workspace_role
from relay_core.db.models.identity import User
from relay_core.db.repositories.audit import AuditLogRepository
from relay_core.db.repositories.llm_calls import LLMCallRepository
from relay_core.db.repositories.policies import WorkspacePolicyRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository, WorkspaceRepository
from relay_core.db.session import get_session
from relay_core.policy.budgets import invalidate_month_spend
from relay_core.policy.engine import ApprovalRules
from relay_core.security.rbac import Role

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_name.lower()).strip("-")
    return slug or "workspace"


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1)


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    plan: str
    monthly_budget_usd: Decimal


class WorkspacePatch(BaseModel):
    monthly_budget_usd: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class AddMemberRequest(BaseModel):
    email: EmailStr
    role: str = Field(pattern="^(owner|admin|member|viewer)$")


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: str


class UpdateMemberRoleRequest(BaseModel):
    role: str = Field(pattern="^(owner|admin|member|viewer)$")


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: CreateWorkspaceRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceOut:
    workspaces = WorkspaceRepository(session)
    base_slug = _slugify(body.name)
    slug = base_slug
    suffix = 1
    while await workspaces.get_by_slug(slug) is not None:
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    workspace = await workspaces.create(name=body.name, slug=slug, created_by=user.id)
    await WorkspaceMemberRepository(session).add(
        workspace_id=workspace.id, user_id=user.id, role=Role.owner.name
    )
    return WorkspaceOut.model_validate(workspace)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceOut:
    workspace = await WorkspaceRepository(session).get(workspace_id)
    if workspace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    return WorkspaceOut.model_validate(workspace)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    body: WorkspacePatch,
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.owner.name)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> WorkspaceOut:
    workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None  # require_workspace_role already 404s a missing workspace
    previous = workspace.monthly_budget_usd
    workspace.monthly_budget_usd = float(body.monthly_budget_usd)
    await AuditLogRepository(session).record(
        workspace_id,
        actor_type="user",
        actor_user_id=current.user.id,
        action="workspace.budget_changed",
        target_type="workspace",
        target_id=workspace_id,
        details={"from": str(previous), "to": str(body.monthly_budget_usd)},
    )
    await session.flush()
    await invalidate_month_spend(redis, workspace_id)
    return WorkspaceOut.model_validate(workspace)


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_members(
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    members = await WorkspaceMemberRepository(session).list_for_workspace(workspace_id)
    users = UserRepository(session)
    out = []
    for member in members:
        member_user = await users.get(member.user_id)
        assert member_user is not None
        out.append(
            MemberOut(
                user_id=member_user.id,
                email=member_user.email,
                full_name=member_user.full_name,
                role=member.role,
            )
        )
    return out


@router.post(
    "/{workspace_id}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED
)
async def add_member(
    body: AddMemberRequest,
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    target = await UserRepository(session).get_by_email(body.email)
    if target is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No Relay account with this email exists yet; they must sign up first",
        )
    members = WorkspaceMemberRepository(session)
    if await members.get(workspace_id, target.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "User is already a member of this workspace")
    member = await members.add(
        workspace_id=workspace_id, user_id=target.id, role=body.role, invited_by=current.user.id
    )
    await AuditLogRepository(session).record(
        workspace_id,
        actor_type="user",
        actor_user_id=current.user.id,
        action="member.added",
        target_type="user",
        target_id=target.id,
        details={"role": body.role},
    )
    return MemberOut(
        user_id=target.id, email=target.email, full_name=target.full_name, role=member.role
    )


@router.patch("/{workspace_id}/members/{user_id}", response_model=MemberOut)
async def update_member_role(
    body: UpdateMemberRoleRequest,
    workspace_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    members = WorkspaceMemberRepository(session)
    member = await members.get(workspace_id, user_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    previous_role, member.role = member.role, body.role
    await AuditLogRepository(session).record(
        workspace_id,
        actor_type="user",
        actor_user_id=current.user.id,
        action="member.role_changed",
        target_type="user",
        target_id=user_id,
        details={"from": previous_role, "to": body.role},
    )
    target = await UserRepository(session).get(user_id)
    assert target is not None
    return MemberOut(
        user_id=target.id, email=target.email, full_name=target.full_name, role=member.role
    )


@router.delete("/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    workspace_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> None:
    await WorkspaceMemberRepository(session).remove(workspace_id, user_id)
    await AuditLogRepository(session).record(
        workspace_id,
        actor_type="user",
        actor_user_id=current.user.id,
        action="member.removed",
        target_type="user",
        target_id=user_id,
    )


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_type: str
    actor_user_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: str | None
    run_id: uuid.UUID | None
    details: dict[str, Any]
    ip: str | None
    created_at: datetime


@router.get("/{workspace_id}/audit-logs", response_model=list[AuditLogOut])
async def list_audit_logs(
    workspace_id: uuid.UUID = Path(...),
    action: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    before: int | None = Query(None, description="id of the last row of the previous page"),
    limit: int = Query(50, ge=1, le=200),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> list[AuditLogOut]:
    """Newest first (section 15.5); admin-only per section 18.2's "View usage & audit logs"."""
    rows = await AuditLogRepository(session).list_for_workspace(
        workspace_id, action=action, actor_user_id=actor_user_id, before=before, limit=limit
    )
    return [AuditLogOut.model_validate(r) for r in rows]


class LLMUsageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    group_key: str
    llm_calls: int
    input_tokens: int
    output_tokens: int
    thought_tokens: int
    cached_tokens: int
    cost_usd: Decimal


class ToolReliabilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    connector_key: str
    status: str
    calls: int


class UsageOut(BaseModel):
    llm_calls: list[LLMUsageOut]
    tool_calls: list[ToolReliabilityOut]


@router.get("/{workspace_id}/usage", response_model=UsageOut)
async def get_usage(
    workspace_id: uuid.UUID = Path(...),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    group_by: Literal["day", "model", "node"] = "day",
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> UsageOut:
    """Section 15.5: the query the cut Grafana dashboards would have answered (ADR-0008).
    Defaults to the current calendar month."""
    llm = await LLMCallRepository(session).usage_breakdown(
        workspace_id, from_=from_, to_=to, group_by=group_by
    )
    tools = await ToolCallRepository(session).reliability_breakdown(
        workspace_id, from_=from_, to_=to
    )
    return UsageOut(
        llm_calls=[LLMUsageOut.model_validate(r) for r in llm],
        tool_calls=[ToolReliabilityOut.model_validate(r) for r in tools],
    )


class RunBudgetIn(BaseModel):
    """Keys match `DEFAULT_RUN_BUDGET`; omitted keys keep their stored value."""

    model_config = ConfigDict(extra="forbid")

    max_steps: int | None = Field(None, ge=1)
    max_tool_calls: int | None = Field(None, ge=1)
    max_llm_calls: int | None = Field(None, ge=1)
    max_cost_usd: float | None = Field(None, gt=0)
    max_wall_seconds: int | None = Field(None, ge=1)


class PolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_budget: RunBudgetIn | None = None
    # Validated strictly here, unlike `parse_approval_rules`' lenient read: a bad rule set is
    # refused at the door rather than stored and silently read back as the default.
    approval_rules: ApprovalRules | None = None
    email_domain_allow: list[str] | None = None
    allow_web_grounding: bool | None = None
    pii_redaction: bool | None = None
    memory_enabled: bool | None = None
    data_retention_days: int | None = Field(None, ge=0)


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_budget: dict[str, Any]
    approval_rules: dict[str, Any]
    email_domain_allow: list[str]
    allow_web_grounding: bool
    pii_redaction: bool
    memory_enabled: bool
    data_retention_days: int
    updated_by: uuid.UUID | None
    updated_at: datetime


@router.get("/{workspace_id}/policy", response_model=PolicyOut)
async def get_policy(
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> PolicyOut:
    return PolicyOut.model_validate(await WorkspacePolicyRepository(session).get(workspace_id))


@router.patch("/{workspace_id}/policy", response_model=PolicyOut)
async def update_policy(
    body: PolicyPatch,
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
) -> PolicyOut:
    policy = await WorkspacePolicyRepository(session).get(workspace_id)
    changes = body.model_dump(exclude_none=True, mode="json")
    if "run_budget" in changes:
        changes["run_budget"] = {**policy.run_budget, **changes["run_budget"]}
    before = {k: getattr(policy, k) for k in changes}
    for field, value in changes.items():
        setattr(policy, field, value)
    policy.updated_by, policy.updated_at = current.user.id, datetime.now(UTC)
    await AuditLogRepository(session).record(
        workspace_id,
        actor_type="user",
        actor_user_id=current.user.id,
        action="policy.updated",
        target_type="workspace_policy",
        target_id=workspace_id,
        details={"from": before, "to": changes},
    )
    await session.flush()
    return PolicyOut.model_validate(policy)
