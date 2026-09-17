"""Connector jobs.

`refresh_oauth_tokens` runs every five minutes (`refresh-oauth-tokens`) and renews any Google
token expiring within fifteen. Tool calls refresh lazily too (`installation_secrets`), so this
sweep exists for the case a lazy refresh can't cover: nobody uses the installation for a while,
its refresh token gets revoked, and an admin should learn that from the connector page rather
than from a run failing.

`sync_all_installations` runs every six hours on Celery beat (`sync-connector-tools`) and once via
`make sync-tools`, which is also how a dev database created before `tool_definitions` existed gets
its rows backfilled. For each active installation in every workspace it re-checks health, then
re-runs tool discovery (`relay_core.tools.sync`): an MCP server's changed tool is disabled for
review, and an installation that was down but has recovered is healthy again.

`recheck_unhealthy_installations` is the same work over a much smaller list, every ten minutes
(Phase 7 E1, section 19.1). Six hours is the right cadence for noticing that a *working*
connector changed its tools; it is far too slow for noticing that a broken one came back, and a
circuit breaker that opens on a transient outage would otherwise leave the installation marked
`degraded` long after the outage ended. Only installations already `degraded` or `down` are
checked, so the short beat costs one indexed query on a healthy deployment.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from relay_core.config import get_settings
from relay_core.connectors.breaker import CircuitBreaker
from relay_core.connectors.oauth import SWEEP_MARGIN_S, refresh_if_expiring
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.session import get_sessionmaker
from relay_core.llm.gateway import build_llm_gateway
from relay_core.security.credential_codec import decrypt_secrets
from relay_core.security.crypto import build_kms
from relay_core.tools.sync import installation_context, installation_secrets, sync_installation
from relay_worker.app import app

logger = logging.getLogger(__name__)


@app.task(name="relay_worker.tasks.connectors.sync_all_installations")  # type: ignore[untyped-decorator]
def sync_all_installations() -> None:
    asyncio.run(_sync_all_installations_async())


@app.task(name="relay_worker.tasks.connectors.recheck_unhealthy_installations")  # type: ignore[untyped-decorator]
def recheck_unhealthy_installations() -> None:
    asyncio.run(_sync_all_installations_async(unhealthy_only=True))


async def _sync_all_installations_async(*, unhealthy_only: bool = False) -> None:
    settings = get_settings()
    kms = build_kms(settings)
    redis = Redis.from_url(settings.redis_url)
    breaker = CircuitBreaker(redis)
    try:
        async with get_sessionmaker()() as session:
            gateway = build_llm_gateway(session, redis, settings)
            installations = ConnectorInstallationRepository(session)
            candidates = (
                await installations.list_unhealthy_across_workspaces()
                if unhealthy_only
                else await installations.list_active_across_workspaces()
            )
            for installation in candidates:
                label = f"{installation.workspace_id}/{installation.slug}"
                try:
                    secrets = await installation_secrets(session, kms, installation, settings)
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
                        # A connector that answers a health check is working, so the breaker's
                        # count of a past outage is stale. Left in place it would take the
                        # installation out of binding again on its next two failures.
                        await breaker.record_success(installation.id)
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


@app.task(name="relay_worker.tasks.connectors.refresh_oauth_tokens")  # type: ignore[untyped-decorator]
def refresh_oauth_tokens() -> None:
    asyncio.run(_refresh_oauth_tokens_async())


async def _refresh_oauth_tokens_async() -> None:
    settings = get_settings()
    kms = build_kms(settings)
    async with get_sessionmaker()() as session:
        credentials = ConnectorCredentialRepository(session)
        horizon = datetime.now(UTC) + timedelta(seconds=SWEEP_MARGIN_S)
        for credential, installation in await credentials.list_expiring_across_workspaces(horizon):
            label = f"{installation.workspace_id}/{installation.slug}"
            try:
                await refresh_if_expiring(
                    session,
                    kms,
                    installation,
                    credential,
                    decrypt_secrets(kms, credential),
                    settings,
                    margin_s=SWEEP_MARGIN_S,
                )
                # Committed per installation, so one dead grant never rolls back another's
                # freshly refreshed token.
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("OAuth refresh failed for %s", label)


if __name__ == "__main__":
    asyncio.run(_sync_all_installations_async())
