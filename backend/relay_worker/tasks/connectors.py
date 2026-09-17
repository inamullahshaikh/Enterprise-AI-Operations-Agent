"""Connector jobs.

`sync_all_installations` re-runs tool discovery (`relay_core.tools.sync`) for every active
installation in every workspace. Phase 6 B3 puts it on the beat schedule; until then
`make sync-tools` runs it once, which is also how a dev database created before
`tool_definitions` existed gets its rows backfilled.
"""

import asyncio

from relay_core.config import get_settings
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.session import get_sessionmaker
from relay_core.security.crypto import build_kms
from relay_core.tools.sync import sync_installation
from relay_worker.app import app


@app.task(name="relay_worker.tasks.connectors.sync_all_installations")  # type: ignore[untyped-decorator]
def sync_all_installations() -> None:
    asyncio.run(_sync_all_installations_async())


async def _sync_all_installations_async() -> None:
    kms = build_kms(get_settings())
    async with get_sessionmaker()() as session:
        installations = ConnectorInstallationRepository(session)
        for installation in await installations.list_active_across_workspaces():
            report = await sync_installation(session, kms, installation)
            # Committed per installation, so one installation's rows never wait on another's.
            await session.commit()
            print(f"{installation.workspace_id}/{installation.slug}: {report.model_dump_json()}")


if __name__ == "__main__":
    asyncio.run(_sync_all_installations_async())
