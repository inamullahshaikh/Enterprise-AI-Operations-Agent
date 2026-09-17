import uuid

from sqlalchemy import select

from relay_core.db.models.tools import ToolDefinition
from relay_core.db.repositories.base import WorkspaceScopedRepository


class ToolDefinitionRepository(WorkspaceScopedRepository[ToolDefinition]):
    model = ToolDefinition

    async def list_for_installation(
        self, workspace_id: uuid.UUID, installation_id: uuid.UUID
    ) -> list[ToolDefinition]:
        stmt = (
            select(ToolDefinition)
            .where(
                ToolDefinition.workspace_id == workspace_id,
                ToolDefinition.installation_id == installation_id,
            )
            .order_by(ToolDefinition.name)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(self, tool: ToolDefinition) -> ToolDefinition:
        """Takes a whole row rather than a dozen keyword arguments: the sync service builds rows
        from a `ToolSpec` plus review state, and a row already carries its own `workspace_id`."""
        self.session.add(tool)
        await self.session.flush()
        return tool

    async def delete(self, workspace_id: uuid.UUID, id_: uuid.UUID) -> None:
        tool = await self.get(workspace_id, id_)
        if tool is not None:
            await self.session.delete(tool)
