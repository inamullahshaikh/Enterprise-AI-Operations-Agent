import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.repositories.base import WorkspaceScopedRepository


class ConnectorInstallationRepository(WorkspaceScopedRepository[ConnectorInstallation]):
    model = ConnectorInstallation

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        connector_key: str,
        name: str,
        slug: str,
        config: dict[str, Any],
        priority: int,
        installed_by: uuid.UUID,
    ) -> ConnectorInstallation:
        installation = ConnectorInstallation(
            workspace_id=workspace_id,
            connector_key=connector_key,
            name=name,
            slug=slug,
            config=config,
            priority=priority,
            installed_by=installed_by,
        )
        self.session.add(installation)
        await self.session.flush()
        return installation

    async def get_by_slug(self, workspace_id: uuid.UUID, slug: str) -> ConnectorInstallation | None:
        stmt = select(ConnectorInstallation).where(
            ConnectorInstallation.workspace_id == workspace_id, ConnectorInstallation.slug == slug
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[ConnectorInstallation]:
        stmt = (
            select(ConnectorInstallation)
            .where(ConnectorInstallation.workspace_id == workspace_id)
            .order_by(ConnectorInstallation.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_active_across_workspaces(self) -> list[ConnectorInstallation]:
        """**Cross-tenant**, like `ApprovalRepository.list_expired_across_workspaces` and for the
        same reason: the tool-sync sweep (`relay_worker.tasks.connectors`) is a system job with
        no requesting user and no workspace to scope to. Each row carries its own
        `workspace_id`, which the sync feeds back into the tenant-scoped methods."""
        stmt = (
            select(ConnectorInstallation)
            .where(ConnectorInstallation.status == "active")
            .order_by(ConnectorInstallation.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_unhealthy_across_workspaces(self) -> list[ConnectorInstallation]:
        """**Cross-tenant**, like `list_active_across_workspaces` above. Feeds the short recovery
        beat (`relay_worker.tasks.connectors.recheck_unhealthy_installations`): an installation
        that went `degraded` or `down` — because it broke, or because its circuit breaker opened
        — should be noticed as recovered within minutes, not at the next six-hourly sweep."""
        stmt = (
            select(ConnectorInstallation)
            .where(
                ConnectorInstallation.status == "active",
                ConnectorInstallation.health.in_(("degraded", "down")),
            )
            .order_by(ConnectorInstallation.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_health(
        self,
        workspace_id: uuid.UUID,
        id_: uuid.UUID,
        *,
        health: str,
        message: str,
        status: str | None = None,
    ) -> ConnectorInstallation:
        installation = await self.get(workspace_id, id_)
        if installation is None:
            raise ValueError(
                f"connector_installations row {id_} not found in workspace {workspace_id}"
            )
        installation.health = health
        installation.health_message = message
        installation.last_health_at = datetime.now(UTC)
        if status is not None:
            installation.status = status
        return installation

    async def delete(self, workspace_id: uuid.UUID, id_: uuid.UUID) -> None:
        installation = await self.get(workspace_id, id_)
        if installation is not None:
            await self.session.delete(installation)
