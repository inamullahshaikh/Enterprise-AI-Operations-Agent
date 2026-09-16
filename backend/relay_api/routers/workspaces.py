"""Workspace & membership routes (docs/system-design.md section 15.2).

Invites are simplified for Phase 1: `POST .../members` adds an *existing* user by
email straight into the workspace. A token-based email-invite flow for people
without an account yet is a UI/notifications feature, not foundational plumbing,
so it's deferred rather than half-built here.
"""

import re
import unicodedata
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import CurrentUser, get_current_user, require_workspace_role
from relay_core.db.models.identity import User
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository, WorkspaceRepository
from relay_core.db.session import get_session
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
    id: uuid.UUID
    name: str
    slug: str
    plan: str


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
    return WorkspaceOut(
        id=workspace.id, name=workspace.name, slug=workspace.slug, plan=workspace.plan
    )


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceOut:
    workspace = await WorkspaceRepository(session).get(workspace_id)
    if workspace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    return WorkspaceOut(
        id=workspace.id, name=workspace.name, slug=workspace.slug, plan=workspace.plan
    )


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
    member.role = body.role
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
