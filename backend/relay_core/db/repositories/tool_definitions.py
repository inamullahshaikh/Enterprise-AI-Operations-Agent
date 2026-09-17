import uuid

from sqlalchemy import select

from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.models.tools import ToolDefinition
from relay_core.db.repositories.base import WorkspaceScopedRepository

_HEALTHY_ENOUGH = ("healthy", "degraded")


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

    async def list_bindable(
        self, workspace_id: uuid.UUID, capabilities: set[str] | None = None
    ) -> list[tuple[ToolDefinition, ConnectorInstallation]]:
        """Enabled rows whose installation is active and healthy or degraded, each with its
        installation, best priority first and newest installation first on a tie (section 7.2
        rule 2). `capabilities` narrows it to rows providing at least one of them; without it
        this is every capability source the resolver counts."""
        stmt = (
            select(ToolDefinition, ConnectorInstallation)
            .join(ConnectorInstallation, ToolDefinition.installation_id == ConnectorInstallation.id)
            .where(
                ToolDefinition.workspace_id == workspace_id,
                ToolDefinition.is_enabled.is_(True),
                ConnectorInstallation.status == "active",
                ConnectorInstallation.health.in_(_HEALTHY_ENOUGH),
            )
            .order_by(
                ConnectorInstallation.priority,
                ConnectorInstallation.created_at.desc(),
                ToolDefinition.name,
            )
        )
        if capabilities is not None:
            stmt = stmt.where(ToolDefinition.capabilities.overlap(sorted(capabilities)))
        return [(row, installation) for row, installation in await self.session.execute(stmt)]

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
