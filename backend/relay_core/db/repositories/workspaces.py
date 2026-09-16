import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.identity import Workspace, WorkspaceMember
from relay_core.db.repositories.base import Repository


class WorkspaceRepository(Repository[Workspace]):
    model = Workspace

    async def get_by_slug(self, slug: str) -> Workspace | None:
        stmt = select(Workspace).where(Workspace.slug == slug)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(self, *, name: str, slug: str, created_by: uuid.UUID) -> Workspace:
        workspace = Workspace(name=name, slug=slug, created_by=created_by)
        self.session.add(workspace)
        await self.session.flush()
        return workspace


class WorkspaceMemberRepository:
    """Membership rows are keyed by (workspace_id, user_id) rather than a surrogate
    `id`, so this doesn't extend `WorkspaceScopedRepository` — but every method below
    still takes `workspace_id` explicitly for the same reason (docs/system-design.md
    section 14.4)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceMember | None:
        return await self.session.get(
            WorkspaceMember, {"workspace_id": workspace_id, "user_id": user_id}
        )

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[WorkspaceMember]:
        stmt = select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_user(self, user_id: uuid.UUID) -> list[WorkspaceMember]:
        stmt = select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_workspaces_for_user(
        self, user_id: uuid.UUID
    ) -> list[tuple[WorkspaceMember, Workspace]]:
        stmt = (
            select(WorkspaceMember, Workspace)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .where(WorkspaceMember.user_id == user_id)
        )
        return [(m, w) for m, w in (await self.session.execute(stmt)).all()]

    async def add(
        self,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        role: str,
        invited_by: uuid.UUID | None = None,
    ) -> WorkspaceMember:
        member = WorkspaceMember(
            workspace_id=workspace_id, user_id=user_id, role=role, invited_by=invited_by
        )
        self.session.add(member)
        await self.session.flush()
        return member

    async def remove(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
        member = await self.get(workspace_id, user_id)
        if member is not None:
            await self.session.delete(member)
