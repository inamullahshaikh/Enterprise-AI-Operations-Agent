"""Capability resolution (docs/system-design.md section 7.2), called from
`relay_core.agent.nodes.load_context`. An installed connector contributes the capabilities of its
enabled `tool_definitions` rows while the installation is active and healthy or degraded; its
manifest's `provides_capabilities` is catalog-only since Phase 6. Custom capabilities
(`custom.*`) come through the same way. "Is this capability available" only needs the union, not
a priority order: picking an installation is `relay_core.tools.registry.ToolRegistry`'s job.

`file_upload` is always available (section 10.8) and contributes `file.read` plus whatever
capabilities its attachments' column names suggest (section 7.2's fallback table, via the
heuristic in `relay_core.connectors.builtin.csv_profile.infer_capabilities`).

An installation whose circuit breaker is open is dropped here exactly as an unhealthy one is
(section 19.1, ADR-0013 decision 7). The filter runs over the rows rather than inside
`list_bindable`'s SQL, because breaker state lives in Redis and the query lives in Postgres —
one `MGET` over the candidates is cheaper than teaching the repository about either.
"""

import uuid

from relay_core.connectors.breaker import CircuitBreaker
from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.models.tools import ToolDefinition
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.documents import DocumentRepository
from relay_core.db.repositories.tool_definitions import ToolDefinitionRepository


async def resolve_available_capabilities(
    tools_repo: ToolDefinitionRepository,
    attachments_repo: AttachmentRepository,
    documents_repo: DocumentRepository,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    breaker: CircuitBreaker | None = None,
) -> list[str]:
    available = set(await always_available(documents_repo, workspace_id))
    rows = await drop_tripped(await tools_repo.list_bindable(workspace_id), breaker)
    for row, _ in rows:
        available.update(row.capabilities)

    attachments = await attachments_repo.list_for_conversation(workspace_id, conversation_id)
    for attachment in attachments:
        available.update(attachment.inferred_capabilities)
    return sorted(available)


async def drop_tripped(
    rows: list[tuple[ToolDefinition, ConnectorInstallation]], breaker: CircuitBreaker | None
) -> list[tuple[ToolDefinition, ConnectorInstallation]]:
    """Removes every row whose installation currently has an open breaker. Shared by this module
    and `relay_core.tools.registry`, so "what is available" and "what binds" can never disagree
    about which installations are out of service."""
    if breaker is None or not rows:
        return rows
    tripped = await breaker.open_among({installation.id for _, installation in rows})
    if not tripped:
        return rows
    return [(row, installation) for row, installation in rows if installation.id not in tripped]


async def always_available(
    documents_repo: DocumentRepository, workspace_id: uuid.UUID
) -> dict[str, str]:
    """Capability -> the always-available connector providing it, with no installation row.
    `documents` (section 10.2/10.8) only counts once something has actually finished ingesting:
    an empty knowledge base shouldn't make the planner think it can search one."""
    sources = {"file.read": "file_upload"}
    if await documents_repo.has_any_ready(workspace_id):
        sources["knowledge.search"] = "documents"
    return sources
