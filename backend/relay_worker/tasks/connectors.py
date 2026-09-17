"""Connector jobs.

`sync_all_installations` runs every six hours on Celery beat (`sync-connector-tools`) and once via
`make sync-tools`, which is also how a dev database created before `tool_definitions` existed gets
its rows backfilled. For each active installation in every workspace it re-checks health, then
re-runs tool discovery (`relay_core.tools.sync`): an MCP server's changed tool is disabled for
review, and an installation that was down but has recovered is healthy again.
"""

import asyncio
import logging

from redis.asyncio import Redis

from relay_core.config import get_settings
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.session import get_sessionmaker
from relay_core.llm.gateway import build_llm_gateway
from relay_core.security.crypto import build_kms
from relay_core.tools.sync import installation_context, installation_secrets, sync_installation
from relay_worker.app import app

logger = logging.getLogger(__name__)


@app.task(name="relay_worker.tasks.connectors.sync_all_installations")  # type: ignore[untyped-decorator]
def sync_all_installations() -> None:
    asyncio.run(_sync_all_installations_async())


async def _sync_all_installations_async() -> None:
    settings = get_settings()
    kms = build_kms(settings)
    redis = Redis.from_url(settings.redis_url)
    try:
        async with get_sessionmaker()() as session:
            gateway = build_llm_gateway(session, redis, settings)
            installations = ConnectorInstallationRepository(session)
            for installation in await installations.list_active_across_workspaces():
                label = f"{installation.workspace_id}/{installation.slug}"
                try:
                    secrets = await installation_secrets(session, kms, installation)
                    ctx = installation_context(
                        installation.workspace_id, installation.installed_by, installation, secrets
                    )
                    connector = CONNECTOR_TYPES[installation.connector_key]()
                    healthy, message = await connector.health_check(ctx)
                    await installations.set_health(
                        installation.workspace_id,
                        installation.id,
                        health="healthy" if healthy else "down",
                        message=message,
                    )
                    if healthy:
                        report = await sync_installation(
                            session, kms, installation, gateway=gateway, settings=settings
                        )
                        message = report.model_dump_json()
                    # Committed per installation, so one's rows never wait on another's.
                    await session.commit()
                    print(f"{label}: {message}")
                except Exception:
                    # One broken installation must not stop the sweep for everyone else.
                    await session.rollback()
                    logger.exception("Tool sync failed for %s", label)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(_sync_all_installations_async())
