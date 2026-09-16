import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.repositories.base import WorkspaceScopedRepository

_ACTIVE_HEALTH = ("healthy", "degraded")


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

    async def list_active_by_connector_keys(
        self, workspace_id: uuid.UUID, connector_keys: set[str]
    ) -> list[ConnectorInstallation]:
        """Active, healthy-or-degraded installations whose connector type is one of
        `connector_keys`, best priority first. Used by `relay_core.tools.registry.ToolRegistry`
        to bind tools for a step's requested capabilities — `connector_keys` is the set of
        connector types whose manifest declares any of them (docs/adr/0009: manifests, not a
        `capability_bindings` table, are the source of truth for what a connector provides).
        """
        if not connector_keys:
            return []
        stmt = (
            select(ConnectorInstallation)
            .where(
                ConnectorInstallation.workspace_id == workspace_id,
                ConnectorInstallation.status == "active",
                ConnectorInstallation.health.in_(_ACTIVE_HEALTH),
                ConnectorInstallation.connector_key.in_(connector_keys),
            )
            .order_by(ConnectorInstallation.priority, ConnectorInstallation.created_at.desc())
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
