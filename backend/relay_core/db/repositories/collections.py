import uuid

from sqlalchemy import select

from relay_core.db.models.documents import Collection
from relay_core.db.repositories.base import WorkspaceScopedRepository

_DEFAULT_NAME = "Knowledge base"


class CollectionRepository(WorkspaceScopedRepository[Collection]):
    model = Collection

    async def get_or_create_default(self, workspace_id: uuid.UUID) -> Collection:
        """Every workspace gets one implicit collection to ingest into until there's an admin
        UI for creating more (docs/system-design.md section 22.1's Knowledge page, a later
        frontend pass) — matching how `file_upload` needs no installation step either."""
        existing = await self.get_by_name(workspace_id, _DEFAULT_NAME)
        if existing is not None:
            return existing
        collection = Collection(workspace_id=workspace_id, name=_DEFAULT_NAME)
        self.session.add(collection)
        await self.session.flush()
        return collection

    async def get_by_name(self, workspace_id: uuid.UUID, name: str) -> Collection | None:
        stmt = select(Collection).where(
            Collection.workspace_id == workspace_id, Collection.name == name
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[Collection]:
        stmt = (
            select(Collection)
            .where(Collection.workspace_id == workspace_id)
            .order_by(Collection.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())
