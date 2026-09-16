"""Repository base classes.

`WorkspaceScopedRepository` requires a `workspace_id` argument on every read/write
it exposes, so a query that forgets to filter by tenant is a type error rather than
a silent cross-tenant data leak (docs/system-design.md section 14.4 and the
"Cross-tenant data access" row of the threat model in section 18.1).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.base import Base, WorkspaceScoped


class Repository[ModelT: Base]:
    """Base for repositories over global (non-tenant) tables, e.g. `users`."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, id_: uuid.UUID) -> ModelT | None:
        return await self.session.get(self.model, id_)


class WorkspaceScopedRepository[WorkspaceModelT: WorkspaceScoped]:
    """Base for repositories over tenant tables. Every method takes `workspace_id`
    explicitly and filters on it — there is no method that can return rows across
    workspaces.
    """

    model: type[WorkspaceModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, workspace_id: uuid.UUID, id_: uuid.UUID) -> WorkspaceModelT | None:
        stmt = select(self.model).where(
            self.model.id == id_,  # type: ignore[attr-defined]
            self.model.workspace_id == workspace_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
